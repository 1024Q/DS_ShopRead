"""Isolated Chromium DOM checks; synthetic credentials never leave local fixtures."""

import asyncio, json, subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import httpx, websockets
from backend import platform_login
from backend.platform_password import login_script

ROOT = Path(__file__).resolve().parents[1]


async def main():
    process = subprocess.Popen(
        [
            str(ROOT / ".browsers/chrome-win64/chrome.exe"),
            "--headless=new",
            "--no-first-run",
            "--remote-debugging-port=19349",
            "--user-data-dir=" + str(ROOT / ".data/login-dom-test"),
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    async with httpx.AsyncClient() as client:
        for _ in range(40):
            try:
                target = next(
                    t for t in (await client.get("http://127.0.0.1:19349/json/list")).json() if t.get("type") == "page"
                )
                break
            except Exception:
                await asyncio.sleep(0.25)
        else:
            process.terminate()
            raise RuntimeError("Test browser unavailable")
    async with websockets.connect(target["webSocketDebuggerUrl"]) as ws:
        counter = 0

        async def call(method, params=None):
            nonlocal counter
            counter += 1
            await ws.send(json.dumps({"id": counter, "method": method, "params": params or {}}))
            while True:
                reply = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if reply.get("id") == counter:
                    if "error" in reply:
                        raise AssertionError(reply["error"])
                    return reply.get("result", {})

        async def evaluate(expression):
            result = await call(
                "Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}
            )
            if "exceptionDetails" in result:
                raise AssertionError(result["exceptionDetails"])
            return result.get("result", {}).get("value")

        async def fixture(html):
            await evaluate("document.body.innerHTML=" + json.dumps(html) + ";window.submitted=0;")

        try:
            await fixture(
                '<div class="shopName--x6nyUylx">蓝怡宝旗舰店<span>⌄</span><div class="dialogHide--test"><span>账号信息</span><span>店铺信息</span><span>页面设置</span><span>退出当前账号</span></div></div><main>订单 商品 经营</main>'
            )
            session = SimpleNamespace(
                evaluate=evaluate,
                shop={"id": 1, "platform": "taobao"},
                state="logging_in",
                store=Mock(),
                auto_window=AsyncMock(),
                authenticated=False,
                auto_login_attempted=False,
            )
            result = await platform_login.refresh(session)
            assert result["name"] == "蓝怡宝旗舰店" and result["logged"], result
            print("PASS Taobao direct shop name excludes account menu")
            await fixture(
                '<input placeholder="邮箱" name="email"><input type="password"><button onclick="window.submitted++">登录</button>'
            )
            result = await evaluate(login_script("fixture@example.invalid", 'test-"quote'))
            assert result.get("submitted") and await evaluate("window.submitted") == 1, result
            assert await evaluate("document.querySelector('input[type=password]').value") == 'test-"quote'
            print("PASS password form fills exact values and submits once")
            await fixture(
                '<input placeholder="账号"><input type="password"><input placeholder="验证码"><button onclick="window.submitted++">登 录</button>'
            )
            result = await evaluate(login_script("fixture", "test"))
            assert result.get("challenge") and await evaluate("window.submitted") == 0, result
            print("PASS captcha form fills credentials without submission")
            await fixture(
                '<input name="account"><input name="email"><input type="password"><button onclick="window.submitted++">登录</button>'
            )
            result = await evaluate(login_script("fixture", "test"))
            assert result.get("manual") and await evaluate("window.submitted") == 0, result
            print("PASS ambiguous account form requires manual selection")
            await fixture('<div onclick="window.submitted++">邮箱登录<span class="active"></span></div>')
            result = await evaluate(login_script("fixture", "test"))
            assert result.get("switched") and await evaluate("window.submitted") == 1, result
            print("PASS Douyin email tab with decorative child switches")
            await fixture("<h1>扫码进入我的小店</h1>")
            assert (await evaluate(login_script("fixture", "test"))).get("missing")
            print("PASS QR-only page never pretends password login is available")
            await fixture('<h1>APP扫一扫登录</h1><img width="160" height="160" src="data:image/png;base64,fixture">')
            assert (await evaluate(platform_login.script("xiaohongshu"))).get("ready")
            print("PASS Xiaohongshu existing QR stays ready without switching")
            await fixture("<h1>打开 抖店App 扫码登录</h1>")
            assert (await evaluate(platform_login.script("douyin"))).get("selected")
            print("PASS Douyin QR page stays selected while image loads")
        finally:
            await call("Browser.close")


asyncio.run(main())
