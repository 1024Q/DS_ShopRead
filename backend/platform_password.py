"""Fill official password forms without exposing credentials in API responses."""

import asyncio
import json
import websockets
from . import platform_login


def login_script(username, password):
    return r"""(()=>{
      const visible=e=>e&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0&&getComputedStyle(e).visibility!=='hidden';
      if(/请验证手机后继续登录|安全验证|拖动.{0,10}滑块|完成.{0,10}拼图|验证码已发送/.test(document.body?.innerText||''))return {manual:true,challenge:true};
      let inputs=[...document.querySelectorAll('input')].filter(e=>visible(e)&&!e.disabled);
      let passwords=inputs.filter(e=>e.type==='password');
      if(!passwords.length){
        const tab=[...document.querySelectorAll('button,a,span,div')].find(e=>visible(e)&&[...e.children].every(c=>!c.textContent.trim())&&/^(密码登录|账号密码登录|账户密码登录|账号登录|账户登录|使用密码登录|邮箱登录)$/.test(e.textContent.trim()))||[...document.querySelectorAll('.icon-password,[title="密码登录"],[aria-label="密码登录"]')].find(visible);
        if(tab){tab.click();return {switched:true};}return {missing:true};
      }
      if(passwords.length!==1)return {manual:true};
      const pw=passwords[0];
      const candidates=inputs.filter(e=>e!==pw&&['text','tel','email'].includes(e.type)&&!e.readOnly&&!/验证码|captcha|verify|code/i.test([e.placeholder,e.name,e.id].join(' ')));
      const users=candidates.filter(e=>/username|loginid|loginname|account|mobile|phone|email|账号|账户|手机|邮箱|会员名|用户名/i.test([e.id,e.name,e.placeholder,e.autocomplete].join(' ')));
      const user=users.length===1?users[0]:candidates.length===1?candidates[0]:null;
      if(!user)return {manual:true};
      const values=VALUES;const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
      [user,pw].forEach((e,i)=>{setter.call(e,values[i]);e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));});
      const challenge=inputs.some(e=>/验证码|captcha|verifycode/i.test([e.placeholder,e.name,e.id].join(' ')));
      if(challenge)return {filled:true,challenge:true};
      const buttons=[...document.querySelectorAll('button,input[type=submit],a,[role=button],div')].filter(e=>visible(e)&&!e.disabled&&(e.tagName!=='DIV'||e.children.length===0)&&/^(登\s*录|立即登录|登录并进入)$/.test((e.value||e.textContent||'').trim()));
      if(buttons.length!==1)return {filled:true,manual:true};
      buttons[0].click();return {submitted:true};
    })()""".replace("VALUES", json.dumps([username, password], ensure_ascii=True))


async def evaluate_login(session, expression):
    """Only evaluate in a shop's allowed top page or official login frame."""
    result = await session.evaluate(expression)
    if result and not result.get("missing"):
        return result
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
                        raise ValueError("官方登录框架暂未就绪")
                    return reply.get("result", {})

        def trusted(frame):
            return platform_login.allowed(session.shop, frame.get("url", "")) and any(
                word in frame.get("url", "").lower() for word in ("login", "passport")
            )

        for frame in (await send("Target.getTargets")).get("targetInfos", []):
            if frame.get("type") != "iframe" or not trusted(frame):
                continue
            attached = None
            try:
                attached = (await send("Target.attachToTarget", {"targetId": frame["targetId"], "flatten": True}))[
                    "sessionId"
                ]
                reply = await send("Runtime.evaluate", {"expression": expression, "returnByValue": True}, attached)
                value = reply.get("result", {}).get("value") or {}
                if value and not value.get("missing"):
                    return value
            finally:
                if attached:
                    await send("Target.detachFromTarget", {"sessionId": attached})

        def frames(node):
            for child in node.get("childFrames", []):
                yield child["frame"]
                yield from frames(child)

        for frame in frames((await send("Page.getFrameTree"))["frameTree"]):
            if not trusted(frame):
                continue
            context = await send("Page.createIsolatedWorld", {"frameId": frame["id"], "worldName": "merchant-password"})
            reply = await send(
                "Runtime.evaluate",
                {"expression": expression, "contextId": context["executionContextId"], "returnByValue": True},
            )
            value = reply.get("result", {}).get("value") or {}
            if value and not value.get("missing"):
                return value
    return {"missing": True}


async def prepare(session):
    if session.shop.get("platform") == "wechat":
        return await platform_login.prepare(session)
    snapshot = await platform_login.refresh(session)
    if snapshot["logged"]:
        return True
    if snapshot.get("verify"):
        session.state = "needs_verification"
        session.recovery_status = "请先在官方画面完成安全验证，再重试登录"
        await session.auto_window(True)
        return False
    credentials = session.store.credentials(session.shop["id"])
    if not credentials.get("password"):
        session.state = "needs_verification"
        session.recovery_status = "请先编辑店铺填写密码，或切换二维码登录"
        await session.auto_window(True)
        return False
    expression = login_script(credentials["username"], credentials["password"])
    for attempt in range(12):
        try:
            result = await evaluate_login(session, expression)
        except ValueError, TimeoutError:
            result = {"missing": True}
        if result.get("submitted") or result.get("filled") or result.get("manual"):
            break
        if attempt == 3 and result.get("missing"):
            # QR-only subpages may lack a back button; reset to the official login entry once.
            await session.command("Page.navigate", {"url": platform_login.home(session.shop)})
        await asyncio.sleep(0.5)
    session.auto_login_attempted = True
    if result.get("submitted"):
        session.state = "logging_in"
        session.recovery_status = "已提交账号密码，等待官方登录结果；如出现验证请在画面完成"
    else:
        session.state = "needs_verification"
        session.recovery_status = (
            "账号密码已填入，请在官方画面完成验证或确认登录"
            if result.get("filled")
            else "请在官方画面完成安全验证，再重试登录"
            if result.get("challenge")
            else "未找到可自动填写的官方密码入口，请在画面选择密码登录，或切换二维码登录"
        )
    await session.auto_window(True)
    return bool(result.get("submitted") or result.get("filled"))
