"""End-to-end control against an isolated animated page, including remote-node relay.

Requires the manager UI on 3212. Never connects to a shop browser or business API.
"""

import asyncio, base64, json, os, subprocess, sys, time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx, uvicorn, websockets
from fastapi import FastAPI, WebSocket
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from backend.remote_control import serve, bridge

ROOT = Path(__file__).resolve().parents[1]


async def main():
    token = os.urandom(24).hex()
    profile = ROOT / ".data/remote-control-fixture"
    profile.mkdir(exist_ok=True)
    chrome = subprocess.Popen(
        [
            str(ROOT / ".browsers/chrome-win64/chrome.exe"),
            "--headless=new",
            "--no-first-run",
            "--remote-debugging-port=19379",
            "--window-size=1200,800",
            "--force-device-scale-factor=1.25",
            "--user-data-dir=" + str(profile),
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    server = None
    server_task = None
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(50):
                try:
                    targets = (await client.get("http://127.0.0.1:19379/json/list")).json()
                    target = next(t for t in targets if t.get("type") == "page")
                    break
                except Exception:
                    await asyncio.sleep(0.2)
            else:
                raise AssertionError("Fixture Chrome not ready")
        async with websockets.connect(
            target["webSocketDebuggerUrl"], proxy=None, max_size=16 * 1024 * 1024
        ) as inspector:
            seq = 0

            async def call(method, params=None):
                nonlocal seq
                seq += 1
                await inspector.send(json.dumps({"id": seq, "method": method, "params": params or {}}))
                while True:
                    m = json.loads(await inspector.recv())
                    if m.get("id") == seq:
                        if "error" in m:
                            raise AssertionError(m["error"])
                        return m.get("result", {})

            html = """<html><body style='margin:0'><button style='position:absolute;left:40px;top:60px;width:200px;height:50px' onclick='window.clicks++'>Test button</button><input id='entry' style='position:absolute;left:40px;top:140px;width:200px;height:50px'><div id='moving' style='position:absolute;top:240px;width:200px;height:100px;background:#126bdf'></div><script>window.clicks=0;let n=0;setInterval(()=>{moving.style.left=(++n%300)+'px'},40)</script></body></html>"""
            await call("Page.navigate", {"url": "data:text/html," + quote(html)})
            await asyncio.sleep(0.4)

            async def get_target():
                return target

            item = SimpleNamespace(
                shop={"id": 777}, control_until=0, retired=False, lock=asyncio.Lock(), target=get_target
            )

            async def cancel_shop(_):
                pass

            business = SimpleNamespace(cancel_shop=cancel_shop, wake=asyncio.Event())
            app = FastAPI()

            @app.websocket("/api/shops/777/control")
            async def endpoint(ws: WebSocket):
                if ws.headers.get("authorization") != "Bearer " + token:
                    await ws.close(code=4401)
                    return
                await serve(ws, item, business)

            @app.websocket("/api/nodes/4/proxy/shops/777/control")
            async def remote(ws: WebSocket):
                if ws.headers.get("authorization") != "Bearer " + token:
                    await ws.close(code=4401)
                    return
                await bridge(ws, "http://127.0.0.1:19422/api/shops/777/control", token)

            server = uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=19422, log_level="warning", timeout_graceful_shutdown=3)
            )
            server_task = asyncio.create_task(server.serve())
            while not server.started:
                await asyncio.sleep(0.05)
            iv = os.urandom(12)
            cipher = AESGCM((ROOT / ".data/ui.key").read_bytes())
            cookie = (
                base64.urlsafe_b64encode(
                    iv
                    + cipher.encrypt(iv, json.dumps({"url": "http://127.0.0.1:19422", "token": token}).encode(), None)
                )
                .decode()
                .rstrip("=")
            )
            start = time.monotonic()
            frames = 0
            acked = False
            clicked = False
            typed = False
            pings = []
            mode = None
            async with websockets.connect(
                "ws://127.0.0.1:3212/browser-control/nodes/4/shops/777",
                origin="http://127.0.0.1:3212",
                additional_headers={"Cookie": "pdd_connection=" + cookie},
                proxy=None,
                max_size=16 * 1024 * 1024,
            ) as viewer:
                while time.monotonic() - start < 5:
                    m = json.loads(await asyncio.wait_for(viewer.recv(), 10))
                    if m["type"] == "mode":
                        mode = m["mode"]
                    elif m["type"] == "frame":
                        frames += 1
                        await viewer.send(json.dumps({"type": "ack", "id": m["id"]}))
                        if frames == 1:
                            print(
                                "PASS remote relay first frame",
                                round((time.monotonic() - start) * 1000),
                                "ms",
                                flush=True,
                            )
                            await viewer.send(
                                json.dumps(
                                    {
                                        "type": "input",
                                        "id": 1,
                                        "events": [
                                            {"type": "mousePressed", "x": 100, "y": 85},
                                            {"type": "mouseReleased", "x": 100, "y": 85},
                                            {"type": "mousePressed", "x": 100, "y": 165},
                                            {"type": "mouseReleased", "x": 100, "y": 165},
                                            {"type": "text", "text": "Remote test 123"},
                                        ],
                                    }
                                )
                            )
                            await viewer.send(json.dumps({"type": "ping", "at": time.monotonic()}))
                    elif m["type"] == "input_ack":
                        acked = True
                    elif m["type"] == "pong":
                        pings.append(round((time.monotonic() - m["at"]) * 1000))
                value = (
                    await call(
                        "Runtime.evaluate",
                        {
                            "expression": '({clicks:window.clicks,text:document.querySelector("#entry").value})',
                            "returnByValue": True,
                        },
                    )
                )["result"]["value"]
                assert value == {"clicks": 1, "text": "Remote test 123"}, value
                assert acked and mode == "stream" and frames >= 10, (acked, mode, frames)
                print(
                    "PASS scaled-coordinate click and text input; frames",
                    frames,
                    "in",
                    round(time.monotonic() - start, 1),
                    "seconds; round trip ms",
                    pings,
                    flush=True,
                )
            for _ in range(100):
                if not item.control_until and not getattr(item, "remote_control_active", False):
                    break
                await asyncio.sleep(0.05)
            assert item.control_until == 0 and business.wake.is_set(), "Control lease did not release"
            print("PASS disconnect stops streaming and restores collection", flush=True)
            await call("Browser.close")
    finally:
        if server:
            server.should_exit = True
        if server_task:
            try:
                await asyncio.wait_for(server_task, 6)
            except TimeoutError:
                server_task.cancel()
                await asyncio.gather(server_task, return_exceptions=True)
        if chrome.poll() is None:
            chrome.terminate()


if __name__ == "__main__":
    asyncio.run(main())
