"""Wait for merchant UI state transitions instead of assuming a click rendered."""

import asyncio, json, time
from .read_errors import PageReadError


def click_script(labels):
    return """(()=>{const labels=LABELS;
      for(const text of labels){
       const all=[...document.querySelectorAll('button,[role=button],a,span,label,div')];
       const candidates=all.filter(e=>e.getClientRects().length&&e.innerText?.trim()===text&&!([...e.children].some(c=>c.innerText?.trim()===text)));
       for(const e of candidates.reverse()){const root=e.closest('button,[role=button],a,label')||e;
       if(root.disabled||root.getAttribute('aria-disabled')==='true')continue;
       root.scrollIntoView({block:'center',inline:'nearest'});const r=root.getBoundingClientRect();
       const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
       if(hit!==root&&!root.contains(hit))continue;
       e.click();return true;}
      }return false})()""".replace("LABELS", json.dumps(labels, ensure_ascii=False))


GUIDE = (
    "(()=>{if(!document.body?.innerText.includes('导出订单可以选择报表类型'))return false;return "
    + click_script(["我知道了"])
    + "})()"
)

# Observed PDD notification panel; never click generic modal confirmation buttons.
NOTICE = """(()=>{for(const e of document.querySelectorAll('[class*=ImportantList_close]')){
 const box=e.closest('[class*=ImportantList_msgbox__]');
 if(!box||!box.innerText.includes('新消息')||!box.innerText.includes('查看全部')||!e.getClientRects().length)continue;
 const r=e.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
 if(hit!==e&&!e.contains(hit))continue;e.click();return true;}return false})()"""

SHIPPING_NOTICE = """(()=>{const text=e=>(e.innerText||'').replace(/\\s/g,'');
 const pattern=/^您有\\d+笔订单未在承诺发货时间内发货或揽收[，,]?请及时处理去处理$/;
 for(const box of document.querySelectorAll('[role=dialog],div')){
 if(!box.getClientRects().length||!pattern.test(text(box)))continue;
 for(const e of box.querySelectorAll('[aria-label=关闭],[aria-label=Close],[data-testid*=close],[class*=close],[class*=Close]')){
 if(!e.getClientRects().length||e.innerText?.trim()==='去处理')continue;
 const r=e.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
 if(hit!==e&&!e.contains(hit))continue;e.click();return true;}}return false})()"""


async def notifications(page):
    if await page.evaluate(NOTICE):
        callback = getattr(page, "export_progress", None)
        if callback:
            callback("已关闭遮挡查询区域的新消息浮层")
    if await page.evaluate(SHIPPING_NOTICE):
        callback = getattr(page, "export_progress", None)
        if callback:
            callback("已关闭发货超时提醒弹窗，继续报表查询")


async def guide(page):
    await notifications(page)
    if await page.evaluate(GUIDE):
        callback = getattr(page, "export_progress", None)
        if callback:
            callback("已关闭延迟出现的首次导出引导")


async def state(page, expression, stage, seconds=25, stable=2):
    deadline = time.monotonic() + seconds
    ready = 0
    while time.monotonic() < deadline:
        try:
            await guide(page)
            value = await page.evaluate(expression)
        except PageReadError:
            value = False
        ready = ready + 1 if value else 0
        if ready >= stable:
            return value
        await asyncio.sleep(0.25)
    raise PageReadError("页面等待超时：" + stage + "；已保留现场，稍后重试")


FIELD_STATE = """(()=>{const skip=new Set(['全选','订单基础信息','订单金额信息','订单发货信息','业务信息','记住本次选择的报表类型','不显示可延迟/暂缓发货订单']);
 const fields=new Map();for(const e of document.querySelectorAll('label')){
 const input=e.querySelector('input[type=checkbox]'),name=e.innerText?.trim().replace(/^商品id$/i,'商品ID');
 if(input&&e.getClientRects().length&&name&&!skip.has(name))fields.set(name,!!input.checked);}
 return {available:[...fields.keys()],checked:[...fields].filter(([_,checked])=>checked).map(([name])=>name)}})()"""


async def fields(page, wanted):
    names = json.dumps(wanted, ensure_ascii=False)
    await state(
        page,
        "(()=>{const s=" + FIELD_STATE + ";return " + names + ".every(n=>s.available.includes(n))})()",
        "等待导出字段完整加载",
    )
    current = await page.evaluate(FIELD_STATE)
    # One click, then read back its state before touching another checkbox.
    for name in current["available"]:
        checked = name in wanted
        if (name in current["checked"]) == checked:
            continue
        callback = getattr(page, "export_progress", None)
        if callback:
            callback(("勾选字段：" if checked else "取消额外字段：") + name)
        expression = (
            "(()=>{const e=[...document.querySelectorAll('label')].find(e=>e.getClientRects().length&&e.querySelector('input[type=checkbox]')&&e.innerText?.trim().replace(/^商品id$/i,'商品ID')==="
            + json.dumps(name, ensure_ascii=False)
            + ");if(!e)return false;const i=e.querySelector('input');if(!i)return false;if(i.checked==="
            + json.dumps(checked)
            + ")return true;return "
            + click_script([name, "商品id"] if name == "商品ID" else [name])
            + "})()"
        )
        deadline = time.monotonic() + 15
        while not await page.evaluate(expression):
            await guide(page)
            if time.monotonic() >= deadline:
                raise PageReadError("字段无法操作：" + name + "；请查看失败截图")
            await asyncio.sleep(0.25)
        await state(
            page,
            "(()=>{const s="
            + FIELD_STATE
            + ";return s.available.includes("
            + json.dumps(name)
            + ")&&s.checked.includes("
            + json.dumps(name)
            + ")==="
            + json.dumps(checked)
            + "})()",
            "回读字段：" + name,
            seconds=10,
        )
        current = await page.evaluate(FIELD_STATE)
    await state(
        page,
        "(()=>{const s="
        + FIELD_STATE
        + ",w="
        + names
        + ";return s.checked.length===w.length&&w.every(n=>s.checked.includes(n))})()",
        "核验全部导出字段已稳定",
    )
