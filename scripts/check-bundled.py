import asyncio
import json
import subprocess
from pathlib import Path
import httpx
import websockets
from backend.browser import chrome_path, ROOT


async def main():
    process = subprocess.Popen(
        [
            chrome_path("bundled"),
            "--headless=new",
            "--no-first-run",
            "--remote-debugging-port=19300",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={ROOT / '.data/browser-smoke-profile'}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            for _ in range(30):
                await asyncio.sleep(0.2)
                try:
                    targets = (await client.get("http://127.0.0.1:19300/json/list")).json()
                    target = next(t for t in targets if t.get("type") == "page")
                    break
                except Exception:
                    pass
            else:
                raise RuntimeError("项目 Chrome 启动失败")
        async with websockets.connect(target["webSocketDebuggerUrl"]) as socket:
            await socket.send(json.dumps({"id": 1, "method": "Page.captureScreenshot", "params": {"format": "jpeg"}}))
            while True:
                result = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
                if result.get("id") == 1:
                    assert result.get("result", {}).get("data"), "截图失败"
                    print("Bundled Chrome: launched, local CDP connected, screenshot captured")
                    break
    finally:
        process.terminate()
        await asyncio.to_thread(process.wait, timeout=10)


asyncio.run(main())
