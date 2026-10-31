"""Official browser login adapters; collection capabilities live in the registry."""

import asyncio, json
from urllib.parse import urlsplit
import websockets

ENTRIES = {
    "pdd": ("https://mms.pinduoduo.com/", ("mms.pinduoduo.com", "cashier.pinduoduo.com"), "拼多多商家版 App"),
    "taobao": ("https://myseller.taobao.com/", ("taobao.com", "tmall.com", "alimama.com"), "淘宝 / 千牛 App"),
    "jd": ("https://shop.jd.com/", ("jd.com",), "京麦 App"),
    "douyin": ("https://fxg.jinritemai.com/login/common", ("jinritemai.com", "douyin.com"), "抖店 App"),
    "taofactory": ("https://tgc.taobao.com/login", ("taobao.com", "tmall.com", "1688.com"), "淘宝 / 千牛 App"),
    "xiaohongshu": ("https://ark.xiaohongshu.com/", ("xiaohongshu.com",), "小红书 App"),
    "wechat": ("https://store.weixin.qq.com/shop", ("weixin.qq.com",), "微信"),
}


def validate_url(value):
    if value:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or len(value) > 2000:
            raise ValueError("请输入有效的 HTTPS 官方登录地址")
    return value


def home(shop):
    return ENTRIES.get(shop.get("platform", "pdd"), (shop.get("login_url", ""), (), ""))[0]


def allowed(shop, url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    domains = ENTRIES.get(
        shop.get("platform", "pdd"), ("", (urlsplit(shop.get("login_url", "")).hostname or "invalid",), "")
    )[1]
    return parsed.scheme == "https" and any(host == d or host.endswith("." + d) for d in domains)


SELECTORS = {
    "taobao": ".icon-qrcode",
    "taofactory": ".icon-qrcode",
    "jd": 'img[alt="扫码icon"]',
    "douyin": ".login-switcher--cell",
    "xiaohongshu": 'img[src*="dfe41f550afa0c73d75d6e393f0ac1f835d94734"]',
}


def script(platform):
    selector = json.dumps(SELECTORS.get(platform, ""))
    return r"""(()=>{const visible=e=>e&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0;const text=document.body?.innerText||'';
    const ready=[...document.querySelectorAll('.qrcode-img img,.qrcode-img canvas,.account-center-qrcode-image-container img,.login__type__container__scan__qrcode,[class*=qrcode] canvas,[class*=qrcode] img,[class*=qr-code] img')].some(e=>visible(e)&&e.getBoundingClientRect().width>100);
    if(ready)return {ready:true};
    if(PLATFORM==='xiaohongshu'&&/APP扫一扫登录/.test(text)&&[...document.querySelectorAll('img[src^="data:image/"]')].some(e=>visible(e)&&e.width>=100&&Math.abs(e.width-e.height)<5))return {ready:true};
    if(PLATFORM==='douyin'&&/打开\s*抖店App\s*扫码登录/.test(text))return {selected:true};
    const selector=SELECTOR;const button=selector?document.querySelector(selector):null;
    if(visible(button)){button.click();return {selected:true};}
    const tab=[...document.querySelectorAll('button,a,span,div')].find(e=>visible(e)&&e.children.length===0&&/^(扫码登录|二维码登录)$/.test(e.textContent.trim()));
    if(tab){tab.click();return {selected:true};}return {ready:false};})()""".replace("SELECTOR", selector).replace(
        "PLATFORM", json.dumps(platform)
    )


async def prepare(session):
    snapshot = await refresh(session)
    if snapshot["logged"]:
        return True
    if session.shop.get("platform") == "taofactory":
        location = await session.evaluate("location.href")
        if "tgcwork.tmall.com/pages/ascm/basic_login" in location:
            if not getattr(session, "qr_bridge_started", False):
                session.qr_bridge_started = True
                await session.command("Page.navigate", {"url": ENTRIES["taobao"][0]})
                session.recovery_status = "先用淘宝 / 千牛扫码，再返回淘工厂继续平台验证"
                return False
            session.recovery_status = "淘宝扫码后已返回淘工厂；如平台仍要求登录，请在官方画面完成账号验证"
            session.state = "needs_verification"
            await session.auto_window(True)
            return True
    expression = script(session.shop.get("platform"))
    result = await session.evaluate(expression)
    selected = bool(result and (result.get("ready") or result.get("selected")))
    # Keep one CDP connection while attaching to cross-process login frames.
    if session.shop.get("platform") in ("taobao", "taofactory") and not selected:
        target = await session.target()
        async with websockets.connect(target["webSocketDebuggerUrl"]) as ws:
            counter = 0

            async def send(method, params=None, session_id=None):
                nonlocal counter
                counter += 1
                message = {"id": counter, "method": method, "params": params or {}}
                if session_id:
                    message["sessionId"] = session_id
                await ws.send(json.dumps(message))
                while True:
                    reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if reply.get("id") == counter:
                        if "error" in reply:
                            raise ValueError("扫码框架暂未就绪")
                        return reply.get("result", {})

            targets = (await send("Target.getTargets")).get("targetInfos", [])
            for frame in targets:
                if (
                    frame.get("type") != "iframe"
                    or not allowed(session.shop, frame.get("url", ""))
                    or "login" not in frame.get("url", "")
                ):
                    continue
                try:
                    attached = await send("Target.attachToTarget", {"targetId": frame["targetId"], "flatten": True})
                    r = await send(
                        "Runtime.evaluate", {"expression": expression, "returnByValue": True}, attached["sessionId"]
                    )
                    v = r.get("result", {}).get("value") or {}
                    selected = v.get("ready") or v.get("selected")
                    await send("Target.detachFromTarget", {"sessionId": attached["sessionId"]})
                    if selected:
                        break
                except ValueError, TimeoutError:
                    pass
            if not selected:
                tree = (await send("Page.getFrameTree"))["frameTree"]

                def frames(node):
                    for child in node.get("childFrames", []):
                        yield child["frame"]
                        yield from frames(child)

                for frame in frames(tree):
                    if not allowed(session.shop, frame["url"]) or "login" not in frame["url"]:
                        continue
                    try:
                        context = await send(
                            "Page.createIsolatedWorld", {"frameId": frame["id"], "worldName": "merchant-qr"}
                        )
                        r = await send(
                            "Runtime.evaluate",
                            {
                                "expression": expression,
                                "contextId": context["executionContextId"],
                                "returnByValue": True,
                            },
                        )
                        v = r.get("result", {}).get("value") or {}
                        selected = v.get("ready") or v.get("selected")
                        if selected:
                            break
                    except ValueError, TimeoutError:
                        pass
    session.state = "needs_verification"
    session.auto_login_attempted = True
    app = ENTRIES.get(session.shop.get("platform"), ("", "", "对应平台 App"))[2]
    session.recovery_status = (
        "请用淘宝 / 千牛扫码；完成后自动返回淘工厂，仍需通过淘工厂官方验证"
        if session.shop.get("platform") == "taofactory"
        else f"请使用{app}扫描官方二维码；若未切换，可在画面中点击扫码入口"
    )
    await session.auto_window(True)
    # A custom platform is controlled interactively because its QR selector is unknown.
    return bool(selected) or session.shop.get("platform") == "other"


async def refresh(session):
    if session.shop.get("platform") == "jd":
        from .jd_login import SNAPSHOT

        snapshot = await session.evaluate(SNAPSHOT)
    else:
        snapshot = await generic_snapshot(session)
    return await apply_snapshot(session, snapshot)


async def generic_snapshot(session):
    snapshot = await session.evaluate(r"""(()=>{const visible=e=>e&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0;const text=document.body?.innerText||'';
    const login=/login|passport|auth/i.test(location.hostname+location.pathname)||/扫码进入我的小店|打开.{0,15}扫码登录|密码登录|短信登录|欢迎登录/.test(text)||[...document.querySelectorAll('input[type=password]')].some(visible);
    const clean=e=>{const own=[...e.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE).map(n=>n.textContent).join('').trim();if(own)return own;const copy=e.cloneNode(true);copy.querySelectorAll('[role=menu],[class*=dialog],[class*=dropdown],[class*=popover],button,svg').forEach(n=>n.remove());return copy.textContent.trim();};
    const selectors=location.hostname==='sycm.taobao.com'?'[class*="Frame-module-header"] [class*="Frame-module-title"]':'[class*=shopName--],[class*=shop-name],[class*=shopName],[class*=store-name],[class*=storeName],[class*=seller-name]';
    const name=[...document.querySelectorAll(selectors)].filter(visible).map(clean).find(t=>t&&t.length<=100&&!/退出当前账号|退出登录|账号信息|页面设置|\n/.test(t))||'';
    const logged=!login&&!!text&&(/退出登录|退出账号|退出当前账号/.test(text)||!!name)&&/商品|订单|店铺|经营/.test(text);
    const verify=/请验证手机后继续登录|安全验证|拖动.{0,10}滑块|完成.{0,10}拼图|验证码已发送/.test(text);
    const failed=/密码错误|账号或密码不正确|账户名或密码错误/.test(text);
    return {logged,name,loginPage:login,signedOut:login,verify,failed,host:location.hostname,header:location.hostname==='one.alimama.com'?text.slice(0,800):''};})()""")
    return snapshot


async def apply_snapshot(session, snapshot):
    if session.shop.get("platform") == "taofactory":
        url = await session.evaluate("location.href")
        if urlsplit(url).hostname == "myseller.taobao.com" and snapshot["logged"]:
            session.qr_bridge_started = True
            await session.command("Page.navigate", {"url": home(session.shop)})
            snapshot = {**snapshot, "logged": False, "name": "", "verify": True}
            session.recovery_status = "扫码完成，正在返回淘工厂验证店铺身份"
    previous = session.state
    if session.shop.get("platform") == "taobao" and not snapshot.get("loginPage"):
        expected = session.shop.get("detected_name") or ""
        if snapshot.get("host") == "one.alimama.com" and expected:
            lines = [line.strip() for line in snapshot.get("header", "").splitlines()]
            if any(line == expected or line.startswith(expected + ":") for line in lines) and any(
                line.startswith(("ID：", "ID:")) for line in lines
            ):
                snapshot = {**snapshot, "logged": True, "name": expected, "verify": False, "signedOut": False}
    if snapshot["logged"]:
        session.state = "logged_in"
        session.recovery_status = ""
        session.error = ""
        if snapshot["name"]:
            if session.shop.get("detected_name") != snapshot["name"]:
                session.store.rename(session.shop["id"], snapshot["name"])
            session.shop["detected_name"] = snapshot["name"]
            session.shop["name"] = session.shop.get("alias") or snapshot["name"]
        if not session.authenticated:
            await session.auto_window(False)
        session.authenticated = True
        session.auto_login_attempted = False
    else:
        session.authenticated = False
        session.state = "needs_verification"
        if snapshot.get("failed"):
            session.state = "login_failed"
            session.error = "账号或密码错误，请在官方画面检查"
        elif not snapshot.get("verify") and session.auto_login_attempted and previous == "logging_in":
            session.state = "logging_in"
        if previous == "logged_in":
            session.recovery_status = "登录状态需重新确认，请打开官方登录画面"
    if previous != session.state:
        session.store.log_event(
            "info",
            "login",
            "state_change",
            "登录成功" if snapshot["logged"] else "等待官方扫码或登录验证",
            session.shop["id"],
        )
    return snapshot
