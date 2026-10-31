import logging
from . import recovery_log
from . import platform_login
from .platform_support import supports
import asyncio
import base64
import json
import os
import platform
import shutil
import subprocess
import time
from urllib.parse import urlsplit
import zipfile
from pathlib import Path
import httpx
import websockets
import weakref
from .store import ROOT
from .read_errors import PageReadError
from .async_database import run as database_work

HOME = "https://mms.pinduoduo.com/"
# Logged-in pages (the steady state polled every few seconds) only read the header's
# textContent; full-page innerText (forces layout) is used only when the header lacks
# the logout entry or the page is not logged in, preserving the original detection.
LOGIN_PROBE = """(() => {const e=document.querySelector('.mms-header__user-info .user-name-name .user-name-text'); const name=e?.textContent.trim()||''; const signedOut=[...document.querySelectorAll('input[type=password]')].some(e=>e.offsetWidth>0);
if(e){const header=e.closest('.mms-header')||e.closest('.mms-header__user-info'); if(header&&header.textContent.includes('退出当前账号'))return {name, logged:true, loginPage:false, signedOut, verify:false, failed:false};}
const text=document.body.innerText; return {name, logged:!!e && text.includes('退出当前账号'), loginPage:location.pathname.includes('/login')&&/账号登录|密码登录|扫码登录|手机登录/.test(text), signedOut, verify:/请验证手机后继续登录|安全验证|拖动.{0,10}滑块|完成.{0,10}拼图|验证码已发送/.test(text), failed:/密码错误|账号或密码不正确|账户名或密码错误/.test(text)};})()"""


def chrome_path(kind):
    if kind == "bundled":
        candidates = (
            list((ROOT / ".browsers").glob("**/chrome.exe"))
            if os.name == "nt"
            else list((ROOT / ".browsers").glob("**/chrome"))
        )
        if candidates:
            return str(candidates[0])
        raise ValueError("项目浏览器尚未安装，请运行安装脚本")
    configured = os.getenv("PDD_CHROME_PATH")
    candidates = [configured, shutil.which("google-chrome"), shutil.which("google-chrome-stable")]
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        candidates.append(str(Path(os.getenv(variable, "")) / "Google/Chrome/Application/chrome.exe"))
    if platform.system() == "Darwin":
        candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise ValueError("未找到系统 Google Chrome")


async def install_browser():
    target = ROOT / ".browsers"
    target.mkdir(exist_ok=True)
    system = platform.system()
    plat = (
        "win64"
        if system == "Windows"
        else ("linux64" if system == "Linux" else ("mac-arm64" if platform.machine() == "arm64" else "mac-x64"))
    )
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        data = (
            (
                await client.get(
                    "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
                )
            )
            .raise_for_status()
            .json()
        )
        url = next(d["url"] for d in data["channels"]["Stable"]["downloads"]["chrome"] if d["platform"] == plat)
        archive = target / "download.zip"
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with archive.open("wb") as output:
                async for chunk in response.aiter_bytes():
                    output.write(chunk)
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            if not (target / member.filename).resolve().is_relative_to(target.resolve()):
                raise ValueError("浏览器压缩包路径异常")
        zipped.extractall(target)
    archive.unlink()
    executable = Path(chrome_path("bundled"))
    if os.name != "nt":
        executable.chmod(0o755)
        for name in ("chrome_crashpad_handler", "chrome_sandbox", "chrome-wrapper"):
            path = executable.parent / name
            if path.exists():
                path.chmod(0o755)
    return str(executable)


_clients = weakref.WeakKeyDictionary()


def local_client():
    """One pooled DevTools HTTP client per event loop (clients cannot cross loops)."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = _clients[loop] = httpx.AsyncClient(timeout=3, trust_env=False)
    return client


class StaleConnection(ConnectionError):
    """The command was never sent; safe to retry on a fresh socket."""


class CDPConnection:
    """Persistent DevTools socket to one page target; concurrent calls are matched by id."""

    def __init__(self, socket, target_id):
        self.socket, self.target_id = socket, target_id
        self.loop = asyncio.get_running_loop()
        self.sequence, self.pending, self.checked = 0, {}, time.monotonic()
        self.reader = asyncio.create_task(self.listen())

    @classmethod
    async def open(cls, target):
        socket = await websockets.connect(
            target["webSocketDebuggerUrl"],
            max_size=16 * 1024 * 1024,
            open_timeout=5,
            close_timeout=2,
            ping_interval=None,
            proxy=None,
        )
        return cls(socket, target.get("id"))

    @property
    def closed(self):
        return self.reader.done() or self.loop is not asyncio.get_running_loop()

    async def listen(self):
        error = ConnectionError("浏览器连接已断开")
        try:
            async for data in self.socket:
                message = json.loads(data)
                future = self.pending.get(message.get("id"))
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(ValueError(message["error"].get("message", "浏览器操作失败")))
                    else:
                        future.set_result(message.get("result", {}))
        except Exception as failure:
            error = failure if isinstance(failure, websockets.ConnectionClosed) else error
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)

    async def call(self, method, params=None, timeout=12):
        if self.closed:
            raise StaleConnection("浏览器连接已断开")
        self.sequence += 1
        key = self.sequence
        future = self.loop.create_future()
        self.pending[key] = future
        try:
            try:
                await self.socket.send(json.dumps({"id": key, "method": method, "params": params or {}}))
            except websockets.ConnectionClosed as error:
                raise StaleConnection("浏览器连接已断开") from error
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(key, None)

    async def close(self):
        if self.loop is not asyncio.get_running_loop():
            return
        try:
            await self.socket.close()
        except Exception:
            recovery_log.suppressed("browser.py:136 恢复步骤未完成", warning=False)
        self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)


class Session:
    CDP_RECHECK_SECONDS = 10

    def __init__(self, shop, store):
        self.shop, self.store = shop, store
        self.port = 9221 + shop["id"]
        if self.port > 65535:
            raise ValueError("店铺数量超过端口范围")
        legacy = ROOT / ".pdd-chrome-profile"
        self.profile = (
            legacy
            if shop["id"] == 1 and shop["browser"] == "system" and legacy.exists()
            else ROOT / ".data/profiles" / str(shop["id"]) / shop["browser"]
        )
        if shop.get("profile_version", 0):
            self.profile = ROOT / ".data/profiles" / str(shop["id"]) / shop["browser"] / f"v{shop['profile_version']}"
        from .browser_recovery import wanted

        self.wanted_running = wanted(store, shop["id"])
        from .browser_sleep import sleeping

        self.sleeping = bool(self.wanted_running and sleeping(store, shop["id"]))
        self.retired = False
        self.data_targets = set()
        self.state = "sleeping" if self.sleeping else "stopped"
        self.visible = False
        # Visibility is explicit and session-local. Debug mode never opens a
        # window or restores a prior session's visible preference automatically.
        self.debug_mode = bool(store.debug_mode(shop["id"])) if hasattr(store, "debug_mode") else False
        preference = store.window_preference(shop["id"])
        self.manual_visibility = False if preference is False else None
        self.error = ""
        from .shop_lock import ShopLock

        self.lock = ShopLock()
        self.process = None
        self.authenticated = False
        self.qr_requested = False
        self.auto_login_attempted = False
        self.last_recovery = 0
        self.recovery_status = ""
        self.control_until = 0
        self.cdp = None
        self.cdp_lock = None
        self.window_controller = None
        self.headless = os.getenv("PDD_HEADLESS", "true" if os.name != "nt" else "false").lower() == "true"

    def public(self):
        return {
            **self.shop,
            "state": self.state,
            "visible": self.visible,
            "error": self.error,
            "adapter_available": bool(platform_login.home(self.shop)),
            "data_adapter_available": supports(self.shop.get("platform", "pdd"), "overview"),
            "port": self.port,
            "headless": self.headless,
            "debug_mode": self.debug_mode,
            "recovery_status": self.recovery_status,
        }

    async def target(self):
        result = await local_client().get(f"http://127.0.0.1:{self.port}/json/list")
        result.raise_for_status()
        pages = [
            p
            for p in result.json()
            if p.get("type") == "page"
            and p.get("id") not in self.data_targets
            and platform_login.allowed(self.shop, p.get("url", ""))
        ]
        pages.sort(
            key=lambda p: urlsplit(p.get("url", "")).hostname != urlsplit(platform_login.home(self.shop)).hostname
        )
        if not pages:
            raise PageReadError("店铺浏览器页面尚未就绪")
        return pages[0]

    async def reclaim_read_pages(self):
        """Reconnect cleanup runs only while owning this node's scheduler lease."""
        from .collector_lease import owns

        if not owns(self.store.directory):
            return
        from .reader_targets import cleanup, has_records

        if not has_records(self):
            return
        from .business_reader import close_reader_target

        try:
            client = local_client()
            response = await client.get(f"http://127.0.0.1:{self.port}/json/version")
            response.raise_for_status()
            browser = response.json()["webSocketDebuggerUrl"]
            response = await client.get(f"http://127.0.0.1:{self.port}/json/list")
            response.raise_for_status()
            targets = {p["id"] for p in response.json() if p.get("type") == "page"}

            async def close(target_id):
                async with websockets.connect(browser, open_timeout=3, close_timeout=1, proxy=None) as socket:
                    await close_reader_target(socket, target_id)

            await cleanup(self, browser, targets, close)
        except OSError, ValueError, KeyError, httpx.HTTPError, websockets.WebSocketException, TimeoutError:
            # Missing/disconnected Chrome is handled by the existing login /
            # wake flow. Unconfirmed records stay available for a later retry.
            import logging

            logging.getLogger(__name__).debug("旧采集页回收暂未完成，保留记录", exc_info=True)

    def _cdp_ready(self):
        cdp = self.cdp
        return cdp if cdp and not cdp.closed and time.monotonic() - cdp.checked < self.CDP_RECHECK_SECONDS else None

    async def connection(self):
        # Reuse one socket; re-select the target periodically so a navigated-away or
        # replaced page is noticed without listing targets on every command.
        if ready := self._cdp_ready():
            return ready
        loop = asyncio.get_running_loop()
        if self.cdp_lock is None or self.cdp_lock[0] is not loop:
            self.cdp_lock = (loop, asyncio.Lock())
        async with self.cdp_lock[1]:
            if ready := self._cdp_ready():
                return ready
            target = await self.target()
            cdp = self.cdp
            if cdp and not cdp.closed and cdp.target_id == target.get("id"):
                cdp.checked = time.monotonic()
                return cdp
            await self.close_cdp()
            self.cdp = await CDPConnection.open(target)
            return self.cdp

    async def close_cdp(self):
        cdp, self.cdp = self.cdp, None
        if cdp:
            await cdp.close()

    async def command(self, method, params=None):
        if self.retired:
            await self.close_cdp()
            raise ValueError("店铺配置已变更，请刷新后重试")
        for attempt in (0, 1):
            cdp = await self.connection()
            try:
                return await cdp.call(method, params)
            except StaleConnection:
                # Never sent (page closed/reloaded since last use): reconnect once.
                if self.cdp is cdp:
                    await self.close_cdp()
                if attempt:
                    raise
            except ValueError:
                raise
            except Exception:
                # Timeout or disconnect after sending: drop the socket, never resend.
                if self.cdp is cdp:
                    await self.close_cdp()
                raise

    async def evaluate(self, expression):
        result = await self.command(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}
        )
        if "exceptionDetails" in result:
            raise PageReadError("页面脚本执行失败")
        return result.get("result", {}).get("value")

    async def start(self, automatic=False):
        if not platform_login.home(self.shop):
            raise ValueError("请先配置此平台的官方登录地址")
        async with self.lock:
            if self.retired:
                raise ValueError("店铺配置已变更，请刷新后重试")
            from .browser_recovery import remember
            from . import update_gate

            if automatic and (not self.wanted_running or update_gate.active()):
                return
            from .browser_sleep import remember as remember_sleep

            remember_sleep(self, False)
            if not automatic:
                remember(self, True)
            await self.reclaim_read_pages()
            launched = False
            try:
                await self.target()
            except Exception:
                launched = True
                if automatic and self.manual_visibility is True:
                    self.manual_visibility = None
                self.auto_login_attempted = False
                self.authenticated = False
                if self.window_controller:
                    self.window_controller.invalidate()
                await self.close_cdp()
                executable = chrome_path(self.shop["browser"])
                self.profile.mkdir(parents=True, exist_ok=True)
                args = [
                    executable,
                    f"--user-data-dir={self.profile}",
                    f"--remote-debugging-port={self.port}",
                    "--remote-debugging-address=127.0.0.1",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-background-timer-throttling",
                    # Hidden (silent) windows must keep rendering: Windows occlusion
                    # tracking would otherwise mark them hidden and pause paint/timers.
                    "--disable-features=CalculateNativeWinOcclusion",
                    "--window-size=1200,850",
                ]
                if self.headless:
                    args += ["--headless=new"]
                from .native_window import passive_launch_options

                # Chrome can ignore STARTUPINFO for its own first window.
                # Let CDP explicitly create that window without activation.
                background_start = os.name == "nt" and not self.headless
                launch_args = args + (["--no-startup-window"] if background_start else [platform_login.home(self.shop)])
                self.process = subprocess.Popen(
                    launch_args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **passive_launch_options(),
                )
                if background_start:
                    await self.create_background_home()
                for _ in range(40):
                    await asyncio.sleep(0.3)
                    try:
                        await self.target()
                        break
                    except Exception:
                        logging.getLogger(__name__).debug("等待浏览器调试端口就绪", exc_info=True)
                else:
                    raise ValueError("浏览器启动超时，请检查运行环境")
                await self.reclaim_read_pages()
            self.state, self.error = "logging_in", ""
            if launched or self.manual_visibility is not None:
                # A freshly launched Chrome window starts visible: hide it at once
                # unless this shop is in debug mode.
                await self.quiet_window(self.launch_visibility())
            if self.qr_requested or self.shop.get("login_method") == "qr":
                await self.wait_for_qr()
                return
            if self.shop.get("platform", "pdd") != "pdd":
                await self.fill_login()
                return
            await self.refresh()
            if not self.authenticated:
                if self.state in ("needs_verification", "login_failed"):
                    if self.qr_requested or self.shop.get("login_method") == "qr":
                        await self.prepare_qr()
                    await self.auto_window(True)
                else:
                    await self.recover_home()

    async def create_background_home(self):
        """Create the first visible Chrome window without activating it.

        Retry discovery only. An ambiguous create never falls back to opening
        the URL through a foreground command-line window.
        """
        for _ in range(40):
            await asyncio.sleep(0.3)
            try:
                response = await local_client().get(f"http://127.0.0.1:{self.port}/json/version")
                response.raise_for_status()
                address = response.json()["webSocketDebuggerUrl"]
                break
            except httpx.HTTPError, ValueError, KeyError:
                continue
        else:
            raise ValueError("浏览器调试端口启动超时，请检查运行环境")
        async with asyncio.timeout(8):
            async with websockets.connect(address, open_timeout=3, close_timeout=1, proxy=None) as socket:
                from .native_window import background_target_bounds

                await socket.send(
                    json.dumps(
                        {
                            "id": 1,
                            "method": "Target.createTarget",
                            "params": {"url": "about:blank", "background": True, "newWindow": True,
                                       "focus": False, **background_target_bounds()},
                        }
                    )
                )
                while True:
                    result = json.loads(await socket.recv())
                    if result.get("id") != 1:
                        continue
                    if "error" in result or not result.get("result", {}).get("targetId"):
                        raise ValueError("浏览器无法后台创建店铺窗口，请重试启动")
                    target_id = result["result"]["targetId"]
                    break
                # Install the native guard before any platform page can execute.
                for attempt in range(20):
                    try:
                        await self.window(self.launch_visibility(), activate=False)
                        break
                    except ValueError:
                        if attempt == 19:
                            raise
                        await asyncio.sleep(0.1)

                async def command(identifier, method, params, session_id=None):
                    message = {"id": identifier, "method": method, "params": params}
                    if session_id:
                        message["sessionId"] = session_id
                    await socket.send(json.dumps(message))
                    while True:
                        response = json.loads(await socket.recv())
                        if response.get("id") == identifier:
                            if "error" in response:
                                raise ValueError("浏览器后台初始化失败，请重试启动")
                            return response.get("result", {})

                attached = await command(2, "Target.attachToTarget", {"targetId": target_id, "flatten": True})
                await command(3, "Page.navigate", {"url": platform_login.home(self.shop)}, attached["sessionId"])
                return target_id

    async def wait_for_qr(self):
        for _ in range(40):
            try:
                if await self.prepare_qr():
                    return True
                snapshot = await self.refresh()
                if snapshot["logged"]:
                    return True
            except ValueError, TimeoutError:
                # Chrome may finish initialization on an empty tab. Navigate only
                # the empty tab in this shop's dedicated profile to the official site.
                pages = (await local_client().get(f"http://127.0.0.1:{self.port}/json/list")).json()
                blank = next(
                    (p for p in pages if p.get("type") == "page" and p.get("url", "") in ("", "about:blank")), None
                )
                if blank:
                    async with websockets.connect(blank["webSocketDebuggerUrl"]) as ws:
                        await ws.send(
                            json.dumps(
                                {"id": 1, "method": "Page.navigate", "params": {"url": platform_login.home(self.shop)}}
                            )
                        )
                        while True:
                            if json.loads(await asyncio.wait_for(ws.recv(), 12)).get("id") == 1:
                                break
            await asyncio.sleep(0.4)
        raise ValueError("官方二维码尚未加载，请稍后重试扫码登录")

    async def prepare_qr(self):
        if self.shop.get("platform", "pdd") != "pdd":
            return await platform_login.prepare(self)
        # Select the official login tab; never generate or proxy a login token.
        selected = await self.evaluate(
            """(()=>{const e=[...document.querySelectorAll('span,div,a')].find(e=>e.children.length===0&&e.textContent.trim()==='扫码登录'&&e.offsetWidth>0);if(!e)return false;e.click();const close=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='已安装去使用');if(close)close.click();return true;})()"""
        )
        if selected:
            self.state = "needs_verification"
            self.auto_login_attempted = True
            self.recovery_status = "请用拼多多商家版 App 扫描官方登录二维码"
            await self.auto_window(True)
        return selected

    async def fill_login(self):
        if self.qr_requested or self.shop.get("login_method") == "qr":
            return await self.prepare_qr()
        if self.shop.get("platform", "pdd") != "pdd":
            from .platform_password import prepare

            return await prepare(self)
        credentials = self.store.credentials(self.shop["id"])
        values = json.dumps([credentials["username"], credentials["password"]], ensure_ascii=True)
        submitted = await self.evaluate(
            """(() => {
          const tab=[...document.querySelectorAll('span,div,a')].find(e=>e.textContent.trim()==='账号登录' && e.children.length===0); if(tab)tab.click();
          const inputs=[...document.querySelectorAll('input')].filter(e=>e.offsetWidth>0);
          const pw=inputs.find(e=>e.type==='password');
          const user=inputs.find(e=>e.id==='usernameId') || inputs.find(e=>e!==pw && /账号|手机|账户/.test(e.placeholder));
          if(!pw||!user)return false;
          const values=VALUES; const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
          [user,pw].forEach((e,i)=>{setter.call(e,values[i]);e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));});
          const button=[...document.querySelectorAll('button')].find(e=>e.offsetWidth>0 && /^登\\s*录$/.test(e.textContent.trim())); if(!button)return false; button.click(); return true;
        })()""".replace("VALUES", values)
        )
        if submitted:
            self.auto_login_attempted = True
        return submitted

    async def recover_home(self):
        checked_at = time.time()
        if self.shop.get("platform", "pdd") != "pdd":
            if self.state in ("needs_verification", "login_failed"):
                await self.auto_window(True)
                return False
            return await self.fill_login()
        """Caller holds the session lock; never interrupt a pending challenge."""
        if self.retired or self.state == "stopped":
            return False
        if self.state in ("needs_verification", "login_failed"):
            await self.auto_window(True)
            return False
        self.last_recovery = time.monotonic()
        self.recovery_status = "返回主页检查登录"
        await database_work(
            self.store.log_event, "warning", "login", "home_check", self.recovery_status, self.shop["id"]
        )
        await self.command("Page.navigate", {"url": HOME + "home/"})
        for _ in range(60):
            await asyncio.sleep(0.3)
            try:
                snapshot = await self.refresh()
            except ValueError, TimeoutError:
                continue
            if snapshot["logged"]:
                self.recovery_status = ""
                self.error = ""
                from .browser_recovery import restored

                await restored(self, checked_at)
                return True
            if self.state in ("needs_verification", "login_failed"):
                if self.qr_requested or self.shop.get("login_method") == "qr":
                    await self.wait_for_qr()
                    return False
                self.recovery_status = "等待人工验证" if self.state == "needs_verification" else "账号登录失败"
                self.error = self.error or "请在登录浏览器完成验证码验证"
                return False
            if (snapshot["signedOut"] or snapshot.get("loginPage")) and not self.auto_login_attempted:
                self.recovery_status = "正在使用保存的账号重新登录"
                await database_work(
                    self.store.log_event, "info", "login", "relogin", self.recovery_status, self.shop["id"]
                )
                await self.auto_window(True)
                if await self.fill_login():
                    self.auto_login_attempted = True
        self.error = "登录仍未完成，请查看登录浏览器" if self.auto_login_attempted else "主页尚未加载完成，请稍后刷新"
        self.recovery_status = self.error
        return False

    def launch_visibility(self):
        """Debug logging never opens a window; only a manual show permits it."""
        return self.manual_visibility is True

    async def quiet_window(self, visible):
        """Best-effort visibility change that never aborts login or collection.

        Automatic shows (debug mode) never take keyboard focus from the user.
        """
        try:
            if visible:
                await self.window(True, activate=False)
            else:
                await self.window(False)
        except Exception:
            logging.getLogger(__name__).debug("浏览器窗口显示状态暂未调整", exc_info=True)

    async def auto_window(self, visible):
        # Authentication checks continue during remote control, without changing
        # the native window's visibility while the user is operating the viewer.
        if getattr(self, "remote_control_active", False):
            return
        # Preserve verification state/prompts, without popping a browser window.
        if self.manual_visibility is None:
            await self.window(False, activate=False)

    async def set_debug_mode(self, enabled):
        """Persist and apply the per-shop debug mode without restarting Chrome."""
        if type(enabled) is not bool:
            raise ValueError("调试模式设置无效")
        self.store.set_debug_mode(self.shop["id"], enabled)
        self.debug_mode = enabled
        if (
            self.state not in ("stopped", "sleeping")
            and not self.retired
            and not getattr(self, "remote_control_active", False)
        ):
            if enabled or self.state not in ("needs_verification", "login_failed"):
                await self.quiet_window(self.manual_visibility is True)

    def remember_visibility(self, visible):
        self.store.set_window_preference(self.shop["id"], visible)
        self.manual_visibility = visible

    async def window(self, visible, activate=True):
        if self.retired:
            raise ValueError("店铺配置已变更，请刷新后重试")
        if os.name == "nt" and not self.headless:
            from .native_window import WindowController

            if self.window_controller is None:
                self.window_controller = WindowController(self.port, self.profile)
            if activate:
                result = await asyncio.to_thread(self.window_controller.apply, visible, self.process)
            else:
                result = await asyncio.to_thread(self.window_controller.apply, visible, self.process, False)
            self.visible = result["visible"]
        else:
            self.visible = visible

    async def guard_windows(self):
        if self.retired:
            raise ValueError("店铺配置已变更，未操作其他窗口")
        if os.name == "nt" and not self.headless:
            from .native_window import WindowController

            if self.window_controller is None:
                self.window_controller = WindowController(self.port, self.profile)
            await asyncio.to_thread(self.window_controller.guard, self.process)
            if not getattr(self, "remote_control_active", False) and self.manual_visibility is not True:
                await self.window(False, activate=False)

    async def maximize(self):
        if self.retired or self.state == "stopped":
            raise ValueError("请先启动店铺浏览器")
        window = await self.command("Browser.getWindowForTarget")
        hidden = not self.visible
        try:
            await self.command(
                "Browser.setWindowBounds", {"windowId": window["windowId"], "bounds": {"windowState": "normal"}}
            )
            await self.command(
                "Browser.setWindowBounds", {"windowId": window["windowId"], "bounds": {"windowState": "maximized"}}
            )
        finally:
            if hidden:
                await self.window(False, activate=False)

    async def refresh(self):
        previous_state = self.state
        checked_at = time.time()
        if self.shop.get("platform", "pdd") != "pdd":
            snapshot = await platform_login.refresh(self)
            if previous_state != "logged_in" and self.state == "logged_in":
                from .browser_recovery import restored

                await restored(self, checked_at)
            return snapshot
        snapshot = await self.evaluate(LOGIN_PROBE)
        if snapshot["logged"]:
            from .shop_name import resolve, complete

            snapshot["name"] = await resolve(self, snapshot["name"])
            self.state = "logged_in"
            self.auto_login_attempted = False
            self.recovery_status = ""
            if snapshot["name"]:
                masked_alias = bool(self.shop.get("alias")) and not complete(self.shop["alias"])
                changed = snapshot["name"][:100] != self.shop.get("detected_name")
                if masked_alias:
                    self.shop["alias"] = ""
                self.shop["detected_name"] = snapshot["name"][:100]
                self.shop["name"] = self.shop.get("alias") or snapshot["name"]
                if changed or masked_alias:
                    await database_work(self.store.rename, self.shop["id"], snapshot["name"])
            if not self.authenticated:
                await self.auto_window(False)
            self.authenticated = True
        elif snapshot["verify"]:
            self.state = "needs_verification"
            if not self.visible:
                await self.auto_window(True)
            self.authenticated = False
        elif snapshot["failed"]:
            self.state = "login_failed"
            self.authenticated = False
            self.error = "账号或密码错误，请在登录画面检查"
        elif snapshot.get("loginPage") and (self.qr_requested or self.shop.get("login_method") == "qr"):
            self.authenticated = False
            self.state = "needs_verification"
            self.recovery_status = "请用拼多多商家版 App 扫码登录"
        elif self.state != "stopped" and (snapshot["signedOut"] or snapshot.get("loginPage") or not self.authenticated):
            self.state = "logging_in"
            if snapshot["signedOut"] or snapshot.get("loginPage"):
                self.authenticated = False
        if previous_state != self.state:
            messages = {
                "logged_in": "登录成功",
                "needs_verification": "需要人工验证码验证",
                "login_failed": "账号登录失败，请检查账号密码",
                "logging_in": "正在检查登录状态",
            }
            await database_work(
                self.store.log_event,
                "error"
                if self.state == "login_failed"
                else "warning"
                if self.state == "needs_verification"
                else "info",
                "login",
                "state_change",
                messages.get(self.state, self.state),
                self.shop["id"],
            )
            if self.state == "logged_in":
                from .browser_recovery import restored

                await restored(self, checked_at)
        return snapshot

    async def screenshot(self):
        result = await self.command("Page.captureScreenshot", {"format": "jpeg", "quality": 75})
        return base64.b64decode(result["data"])

    async def stop(self, sleep=False):
        from .read_page_pool import close_idle

        if hasattr(self, "store") and not sleep:
            from .browser_sleep import remember as remember_sleep

            remember_sleep(self, False)
            from .browser_recovery import remember

            remember(self, False)

        await close_idle(self)

        # Close the browser itself, even when its merchant page disappeared.
        # A disconnect during Browser.close is expected; confirm using a fresh HTTP connection.
        async def stopped():
            await self.close_cdp()
            if self.window_controller:
                self.window_controller.invalidate()
            self.state, self.visible, self.authenticated, self.error = (
                "sleeping" if sleep else "stopped",
                False,
                False,
                "",
            )
            if sleep:
                from .browser_sleep import remember as remember_sleep

                remember_sleep(self, True)

        async with httpx.AsyncClient(timeout=httpx.Timeout(2, connect=5), trust_env=False) as client:
            try:
                version = await client.get(f"http://127.0.0.1:{self.port}/json/version")
                version.raise_for_status()
                socket_url = version.json()["webSocketDebuggerUrl"]
            except httpx.ConnectError:
                await stopped()
                return
            try:
                async with websockets.connect(socket_url, open_timeout=3, close_timeout=1, proxy=None) as socket:
                    await socket.send(json.dumps({"id": 1, "method": "Browser.close"}))
                    await asyncio.wait_for(socket.recv(), 3)
            except websockets.WebSocketException, ConnectionError, OSError, TimeoutError:
                pass
        await self.close_cdp()
        async with httpx.AsyncClient(timeout=httpx.Timeout(2, connect=5), trust_env=False) as client:
            for _ in range(25):
                await asyncio.sleep(0.2)
                try:
                    await client.get(f"http://127.0.0.1:{self.port}/json/version")
                except httpx.ConnectError:
                    await stopped()
                    return
                except httpx.ReadError, httpx.RemoteProtocolError, httpx.TimeoutException:
                    continue  # Closing can reset an existing connection before the port disappears.
        raise ValueError("无法确认原浏览器已关闭，暂不切换节点；请关闭该店铺窗口后重试")


if __name__ == "__main__":
    print(asyncio.run(install_browser()))
