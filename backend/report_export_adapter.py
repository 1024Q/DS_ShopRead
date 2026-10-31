"""PDD's native form submission; export list/download APIs observed on its page."""

import logging
import asyncio, json
from datetime import datetime
from urllib.parse import urlsplit
import httpx
from .orders import OrdersPage as BaseOrdersPage
from .business_reader import checked_result, BEIJING
from .report_export_store import FIELDS, LEGACY_FIELDS, MAX_BYTES
from .read_errors import PageReadError
from . import report_export_ui as ui

LIST = "/mars/shop/orders/export/task/list"
URL = "/mars/shop/orders/export/task/getUrl"
REPORT_PAGE = "https://mms.pinduoduo.com/orders/exportExcel?exportType=0"
# IDs verified in the native field dialog's infoList and task/list.
FIELD_IDS = {1, 2, 3, 13, 24, 25, 12, 30}
NEW_FIELD_IDS = FIELD_IDS | {21, 22}  # 商品id、商品规格


def canonical_field(value):
    return "商品ID" if str(value).strip().lower() == "商品id" else str(value).strip()


FIBER = "e[Object.keys(e).find(k=>k.startsWith('__reactFiber$')||k.startsWith('__reactInternalInstance$'))]"


class SubmissionRejected(ValueError):
    pass


class OrdersPage(BaseOrdersPage):
    """Observe the native generation response, without replaying a write API."""

    # Chrome hidden targets support DOM/input/download but have no compositor
    # surface for error screenshots. Create this page in an offscreen native
    # window from the outset; never show it on the desktop for rendering.
    requires_native_surface = True

    def __init__(self, session):
        super().__init__(session)
        self.export_requests = set()
        self.export_responses = asyncio.Queue()
        self.submission_armed = False

    async def __aenter__(self):
        page = await super().__aenter__()
        try:
            await self.call("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Shanghai"})
        except BaseException:
            await self.__aexit__()
            raise
        return page

    def observe(self, message):
        super().observe(message)
        params = message.get("params", {})
        method = message.get("method")
        if method == "Network.requestWillBeSent" and self.submission_armed:
            request = params["request"]
            url = urlsplit(request["url"])
            if (
                url.hostname == "mms.pinduoduo.com"
                and request.get("method") == "POST"
                and "/export/" in url.path
                and url.path not in (LIST, URL)
            ):
                self.export_requests.add(params["requestId"])
        if method == "Network.loadingFinished" and params["requestId"] in self.export_requests:
            self.export_requests.discard(params["requestId"])
            task = asyncio.create_task(self.export_response(params["requestId"]))
            self.body_tasks.add(task)
            task.add_done_callback(self.body_tasks.discard)

    async def export_response(self, rid):
        try:
            response = await self.call("Network.getResponseBody", {"requestId": rid})
            self.export_responses.put_nowait(json.loads(response["body"]))
        except Exception:
            logging.getLogger(__name__).debug("读取导出接口响应失败", exc_info=True)


def note(page, message):
    callback = getattr(page, "export_progress", None)
    if callback:
        callback(message)


async def submit(page):
    # Do not let a matching span or disabled button masquerade as submission.
    try:
        await ready_to_submit(page)
    except ValueError as error:
        raise SubmissionRejected(str(error)) from None
    page.submission_armed = True
    try:
        clicked = await page.evaluate(
            "(()=>{const a=[...document.querySelectorAll('button,[role=button]')].filter(e=>e.getClientRects().length&&e.innerText?.trim()==='生成报表'&&!e.disabled&&e.getAttribute('aria-disabled')!=='true');if(a.length!==1)return false;a[0].click();return true})()"
        )
        if clicked is not True:
            raise SubmissionRejected("生成按钮不可用，未执行提交")
        try:
            response = await asyncio.wait_for(page.export_responses.get(), 25)
        except TimeoutError:
            raise PageReadError("生成按钮已点击，但未收到平台受理响应；提交结果待核验，不会自动重复提交") from None
        if response.get("success") is False:
            raise SubmissionRejected(
                str(response.get("errorMsg") or response.get("error_msg") or "平台拒绝生成报表")[:200]
            )
        return checked_result(response)
    finally:
        page.submission_armed = False


async def ready_to_submit(page):
    return await ui.state(
        page,
        "(()=>{const a=[...document.querySelectorAll('button,[role=button]')].filter(e=>e.getClientRects().length&&e.innerText?.trim()==='生成报表'&&!e.disabled&&e.getAttribute('aria-disabled')!=='true');return a.length===1})()",
        "等待生成报表按钮可用",
    )


async def wait(page, expression, seconds=25):
    for _ in range(seconds * 2):
        value = await page.evaluate(expression)
        if value:
            return value
        await asyncio.sleep(0.5)
    raise ValueError("平台页面控件未就绪：请检查登录、验证码或页面权限")


async def click(page, text):
    return await export_click(page, [text])


EXPORT_GUIDE = ui.GUIDE


async def export_click(page, labels):
    """Handle only the known export tutorial; never acknowledge other warnings."""
    expression = ui.click_script(labels)
    for _ in range(50):
        await ui.notifications(page)
        if await page.evaluate(EXPORT_GUIDE):
            note(page, "已关闭拼多多首次导出引导“我知道了”")
            await ui.state(
                page, "!document.body?.innerText.includes('导出订单可以选择报表类型')", "等待首次导出引导关闭"
            )
        if await page.evaluate(expression):
            return
        await asyncio.sleep(0.5)
    raise ValueError("导出步骤未就绪：" + " / ".join(labels) + "；请查看失败截图")


async def post(page, path, body):
    value = await page.evaluate(
        "(async()=>{const r=await fetch("
        + json.dumps(path)
        + ",{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify("
        + json.dumps(body)
        + "),signal:AbortSignal.timeout(20000)});return await r.json()})()"
    )
    return checked_result(value)


async def reports(page):
    rows = []
    for number in range(1, 6):
        result = await post(page, LIST, {"pageNumber": number, "pageSize": 40, "exportType": 0})
        items = result.get("pageItems")
        if not isinstance(items, list):
            raise ValueError("平台报表列表结构变化，请检查适配")
        rows.extend(items)
        if len(rows) >= result.get("totalItemNum", len(rows)):
            return rows
    raise ValueError("平台报表超过 200 项，请人工定位，防止误匹配报表")


def epoch(value):
    try:
        value = float(value)
        return int(value / 1000 if value > 10**11 else value)
    except TypeError, ValueError, OverflowError:
        return 0


def match_errors(row, job):
    start = int(datetime.fromisoformat(job["start"]).replace(tzinfo=BEIJING).timestamp())
    end = int(datetime.fromisoformat(job["end"]).replace(tzinfo=BEIJING).timestamp())
    titles = row.get("titles") or []
    expected = job.get("fields", LEGACY_FIELDS)
    ids = set(
        job.get("field_ids")
        or (FIELD_IDS if set(expected) == set(LEGACY_FIELDS) else NEW_FIELD_IDS if set(expected) == set(FIELDS) else ())
    )
    fields_match = (
        set(map(canonical_field, titles)) == set(expected)
        or all(type(v) is int or isinstance(v, str) and v.isdigit() for v in titles)
        and bool(ids)
        and {int(v) for v in titles} == ids
    )
    errors = []
    if str(row.get("id")) in job.get("before_ids", []):
        errors.append("提交前已存在")
    if epoch(row.get("groupStartTime")) != start or epoch(row.get("groupEndTime")) != end:
        errors.append("订单时间范围不一致（请核对节点时区）")
    if str(row.get("orderType")) != "0" or str(row.get("afterSaleType")) != "1":
        errors.append("订单或售后状态不一致")
    # A complete pre-submit ID snapshot and confirmed native receipt establish
    # chronology without comparing the node clock with the platform clock.
    has_baseline = job.get("submission_confirmed") is True and isinstance(job.get("before_ids"), list)
    if not has_baseline and epoch(row.get("createdTime")) < epoch(job["submitted_at"]) - 3:
        errors.append("平台生成时间早于提交时间")
    if not fields_match:
        errors.append("导出字段不一致")
    return errors


def match(row, job):
    return not match_errors(row, job)


def match_diagnostics(rows, job):
    candidates = [row for row in rows if str(row.get("id")) not in job.get("before_ids", [])]
    return [
        {
            "id": str(row.get("id")),
            "reasons": match_errors(row, job),
            "start": row.get("groupStartTime"),
            "end": row.get("groupEndTime"),
            "created": row.get("createdTime"),
            "field_count": len(row.get("titles") or []),
        }
        for row in candidates[:5]
    ]


async def open_reports(page):
    await page.call("Page.navigate", {"url": REPORT_PAGE})
    # Navigation acknowledges before the new document/body/context is available.
    for _ in range(50):
        await asyncio.sleep(0.5)
        try:
            state = await page.evaluate(
                "({login:location.pathname.includes('/login'),ready:location.pathname==='/orders/exportExcel'&&document.readyState!=='loading'&&!!document.body?.innerText.includes('已生成报表')})"
            )
        except PageReadError:
            continue
        if state.get("login"):
            raise ValueError("报表页面需要登录，请完成店铺登录后恢复下载")
        if state.get("ready"):
            return
    raise PageReadError("生成报表页面尚未加载完成，将重试读取原报表，不会重复生成")


async def configure(page, job):
    fields = job.get("fields", LEGACY_FIELDS)
    note(page, "打开订单查询，选择全部订单（近3个月）")
    await page.open("orders")
    await wait(page, "[...document.querySelectorAll('button')].some(e=>e.innerText==='批量导出')")
    # Select the all-orders tab without enabling background full-order collection.
    await wait(
        page,
        "(()=>{let e=[...document.querySelectorAll('*')].find(e=>e.getClientRects().length&&/^全部订单[（(]近3个月[）)]$/.test(e.textContent.trim()));if(!e)return false;e.click();return true})()",
    )
    await click(page, "重置")
    note(page, "设置订单状态与售后状态")
    for marker, target in [("待发货", "全部"), ("无售后/取消售后", "无售后/取消售后")]:
        script = """(()=>{for(const e of document.querySelectorAll('input')){let f=FIBER;
          for(let n=0;f&&n<20;n++,f=f.return){const p=f.memoizedProps||{};
           if(p.options?.some(o=>o.label===MARKER)&&typeof p.onChange==='function'){
            const o=p.options.find(o=>o.label===TARGET);if(!o)return false;p.onChange(o.value);return true;}}}return false})()"""
        await wait(
            page,
            script.replace("FIBER", FIBER).replace("MARKER", json.dumps(marker)).replace("TARGET", json.dumps(target)),
        )
        verify = script.replace("p.onChange(o.value);return true;", "return p.value===o.value;")
        await ui.state(
            page,
            verify.replace("FIBER", FIBER).replace("MARKER", json.dumps(marker)).replace("TARGET", json.dumps(target)),
            "核验筛选条件：" + target,
        )
    times = [int(datetime.fromisoformat(job[k]).replace(tzinfo=BEIJING).timestamp() * 1000) for k in ("start", "end")]
    note(page, "设置月份时间范围")
    script = """(()=>{const e=document.querySelector('input[data-testid=beast-core-rangePicker-htmlInput]');if(!e)return false;
      let f=FIBER,last;for(let n=0;f&&n<25;n++,f=f.return){const p=f.memoizedProps||{};if(Array.isArray(p.value)&&p.value.length===2&&typeof p.onChange==='function')last=p;}
      if(!last)return false;const v=last.value;last.onChange(TIMES.map((t,i)=>v[i] instanceof Date?new Date(t):v[i]?.clone?.().add(t-v[i].valueOf(),'millisecond')));return true})()"""
    await wait(page, script.replace("FIBER", FIBER).replace("TIMES", json.dumps(times)))
    verify = """(()=>{const e=document.querySelector('input[data-testid=beast-core-rangePicker-htmlInput]');if(!e)return false;
      let f=FIBER;for(let n=0;f&&n<25;n++,f=f.return){const p=f.memoizedProps||{};
      if(Array.isArray(p.value)&&p.value.length===2&&typeof p.onChange==='function'&&p.value.every((v,i)=>v?.valueOf()===TIMES[i]))return true;}return false})()"""
    await ui.state(page, verify.replace("FIBER", FIBER).replace("TIMES", json.dumps(times)), "核验自定义起止时间已生效")
    while not page.order_responses.empty():
        page.order_responses.get_nowait()
    await click(page, "查询")
    note(page, "等待订单查询响应，核验时间及筛选条件")
    try:
        async with asyncio.timeout(30):
            while True:
                body, response = await page.order_responses.get()
                if (
                    body.get("orderType") == 0
                    and body.get("afterSaleType") == 1
                    and body.get("groupStartTime") == times[0] // 1000
                    and body.get("groupEndTime") == times[1] // 1000
                ):
                    checked_result(response)
                    break
    except TimeoutError:
        raise PageReadError("查询响应等待超时：尚未核验指定订单范围，未提交生成") from None
    note(page, "打开批量导出")
    await click(page, "批量导出")
    note(page, "选择自定义报表，处理首次导出引导")
    await export_click(page, ["自定义报表"])
    note(page, "打开字段弹窗，等待页面可操作")
    await export_click(page, ["配置字段", "添加报表字段", "修改报表字段"])
    await ui.fields(page, fields)
    note(page, "确认字段，等待配置弹窗关闭")
    await click(page, "确认")
    await ui.state(
        page, "!(" + ui.FIELD_STATE + ").available.includes(" + json.dumps(fields[0]) + ")", "等待字段配置弹窗关闭"
    )
    note(page, f"{len(fields)} 个导出字段已核验，等待生成")


async def download(url, path):
    def valid(u):
        p = urlsplit(u)
        host = p.hostname or ""
        return (
            p.scheme == "https"
            and not p.username
            and (host == "pfs.yangkeduo.com" or host.endswith(".pddpic.com") or host.endswith(".pinduoduo.com"))
        )

    async with httpx.AsyncClient(timeout=90, trust_env=False, follow_redirects=False) as client:
        for _ in range(4):
            if not valid(url):
                raise ValueError("平台返回的下载域名不在允许范围，请人工核对")
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    from urllib.parse import urljoin

                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                size = 0
                with open(path, "wb") as out:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError("报表超过 512 MB，请缩小导出时间范围")
                        out.write(chunk)
                if not size:
                    raise ValueError("平台返回空文件")
                return
        raise ValueError("报表下载重定向次数异常")
