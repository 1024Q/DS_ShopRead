"""Promotion queries observed from native read-only controls; no campaign mutations."""

from .collection_metrics import measured
import asyncio
import time
import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from .business_reader import ReadPage, BEIJING, checked_result, date_text
from .product_images import thumbnail
from .read_errors import PageReadError
from .sync_progress import report

KINDS = {"ad_products", "ad_reports"}
URLS = {
    "ad_products": "https://yingxiao.pinduoduo.com/goods/promotion/list",
    "ad_reports": "https://yingxiao.pinduoduo.com/goods/report/promotion/overView",
}
LIST = "/mms-gateway/venus/api/goods/promotion/v3/list"
ENTITY = "/mms-gateway/poseidon/api/report/queryEntityReport"
LOGS = "/mms-gateway/venus/api/log/queryOperationLogWithCount"
SUMMARY = "/mms-gateway/poseidon/api/report/queryHourlyRangeReport"
RANGE_SUMMARY = "/mms-gateway/poseidon/api/report/queryRangeReport"
SUMMARY_PATHS = {SUMMARY, RANGE_SUMMARY}
RANGES = {"realtime": "今日", "yesterday": "昨日", "week": "近 7 日", "month": "近 30 日", "quarter": "近 90 日"}
FIELDS = {
    "ad_spend": "orderSpend",
    "total_spend": "spend",
    "ad_revenue": "gmv",
    "net_revenue": "netGmv",
    "settlement_revenue": "settlementGmv",
    "roi": "orderSpendRoiUnified",
    "net_roi": "orderSpendNetRoi",
    "settlement_roi": "settlementRoi",
    "ad_orders": "orderNum",
    "net_orders": "netOrderNum",
    "impressions": "impression",
    "clicks": "click",
    "ctr": "ctr",
    "cvr": "cvr",
    "cost_per_order": "costPerOrder",
}
MONEY = {"ad_spend", "total_spend", "ad_revenue", "net_revenue", "settlement_revenue", "cost_per_order"}
LIMIT = 2000


def options_for(trade_range="realtime", today=None):
    if trade_range not in RANGES:
        raise ValueError("推广统计范围无效")
    today = today or datetime.now(BEIJING).date()
    end = today if trade_range == "realtime" else today - timedelta(days=1)
    days = {"week": 7, "month": 30, "quarter": 90}.get(trade_range, 1)
    return {
        "trade_range": trade_range,
        "date_from": (end - timedelta(days=days - 1)).isoformat(),
        "date_to": end.isoformat(),
        "stars": [],
    }


def number(value, unit=None):
    if value is None or value in ("", "--", "—", "-"):
        return None
    if isinstance(value, dict):
        if unit and value.get("unit") != unit:
            raise ValueError("推广数据单位发生变化，保留上次数据")
        value = value.get("value")
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("推广数据数字格式异常")
    try:
        n = Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        raise ValueError("推广数据数字格式异常") from None
    if not n.is_finite():
        raise ValueError("推广数据包含无效数字")
    return float(n)


def fields(raw):
    if not isinstance(raw, dict):
        raise ValueError("推广统计字段发生变化")
    result = {
        key: number(
            raw.get(source),
            "YUAN"
            if key in MONEY
            else "PER_ONE"
            if key.endswith("roi")
            else "PERCENT"
            if key in ("ctr", "cvr")
            else None,
        )
        for key, source in FIELDS.items()
    }
    if result["ad_spend"] == 0:
        for key in ("roi", "net_roi", "settlement_roi"):
            result[key] = None
    return result


def report_time(summary):
    value = (
        summary.get("reportLatestUpdateTime") or summary.get("reportLastUpdateTime") or summary.get("lastUpdateTime")
    )
    if not value:
        return None
    try:
        return date_text(value)
    except TypeError, ValueError, OverflowError, OSError:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return (parsed.astimezone(BEIJING) if parsed.tzinfo else parsed).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def summarize(rows):
    result = {}
    for field in FIELDS:
        values = [Decimal(str(r[field])) for r in rows if r.get(field) is not None]
        result[field] = float(sum(values)) if values else None
    for target, numerator, denominator, mult in [
        ("roi", "ad_revenue", "ad_spend", 1),
        ("net_roi", "net_revenue", "ad_spend", 1),
        ("settlement_roi", "settlement_revenue", "ad_spend", 1),
        ("ctr", "clicks", "impressions", 100),
        ("cvr", "ad_orders", "clicks", 100),
        ("cost_per_order", "ad_spend", "ad_orders", 1),
    ]:
        paired = [r for r in rows if r.get(numerator) is not None and r.get(denominator) is not None]
        total = sum(Decimal(str(r[denominator])) for r in paired)
        result[target] = float(sum(Decimal(str(r[numerator])) for r in paired) / total * mult) if total > 0 else None
    return result


def product(raw, kind, mode, options):
    info = raw.get("goodsInfo") if kind == "ad_products" else raw.get("externalFieldValues")
    if not isinstance(info, dict):
        raise ValueError("推广商品字段发生变化")
    identifier = raw.get("adId") or raw.get("entityId")
    goods_id = raw.get("goodsId") or info.get("goodsId")
    if identifier is None or not goods_id:
        raise ValueError("推广记录缺少商品或推广编号")
    status = raw.get("adStatus")
    if kind == "ad_reports":
        status_text = "已删除推广" if str(info.get("adDeleted")) == "1" else "报表记录"
    else:
        from .promotion_status import label

        status_text = label(status)
    budget = number(raw.get("maxCost"), "YUAN")
    return {
        "id": str(identifier),
        "goods_id": str(goods_id),
        "name": info.get("goodsName") or raw.get("adName") or info.get("adName") or "",
        "thumbnail_url": thumbnail(info),
        "status": status_text,
        "status_code": status,
        "promotion_type": "全店托管" if mode == 3 else "稳定成本推广" if mode == 1 else "平台报表",
        "target_roi": number((raw if kind == "ad_products" else info).get("targetRoi"), "PER_ONE"),
        "budget": budget,
        "budget_text": "不限" if budget == 1000000 else None,
        "created_at": options["date_to"],
        "period": options["date_from"] + " 至 " + options["date_to"],
        **fields(raw.get("reportInfo") or {} if kind == "ad_products" else raw),
    }


class PromotionPage(ReadPage):
    def __init__(self, session):
        super().__init__(session)
        self.ad_requests = {}
        self.events = []
        self.changed = asyncio.Event()
        # (path, begin, end, monotonic) of every matching request the page sent;
        # used to tell "query never fired" from "wrong dates" and "slow response".
        self.sent_scopes = []

    def reset_buffers(self):
        super().reset_buffers()
        self.ad_requests.clear()
        self.events.clear()
        self.changed.clear()
        self.sent_scopes.clear()

    def note_request(self, path, scope):
        start = request_date(scope.get("beginDate") or scope.get("startDate"))
        end = request_date(scope.get("endDate"))
        self.sent_scopes.append((path, start, end, time.monotonic()))
        self.sent_scopes = self.sent_scopes[-60:]
        self.changed.set()

    async def range_state(self, label):
        """Read the date control: is the requested range option actually selected?"""
        try:
            return (
                await asyncio.wait_for(
                    self.evaluate(
                        "(()=>{const login=location.pathname.includes('login');"
                        "const e=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&e.textContent.trim()==="
                        + json.dumps(label)
                        + "&&e.offsetWidth>0);if(!e)return {login,found:false,selected:null};"
                        "let n=e,selected=false;for(let i=0;n&&i<5;i++,n=n.parentElement){const c=String(n.className||'');"
                        "if(/(^|[-_ ])(active|selected|checked)/i.test(c)||n.getAttribute('aria-checked')==='true'||"
                        "n.getAttribute('aria-selected')==='true'){selected=true;break;}}"
                        "return {login,found:true,selected};})()"
                    ),
                    3,
                )
                or {}
            )
        except Exception:
            return {}

    def reuse_ready_expression(self, kind):
        route = urlsplit(URLS[kind])
        return (
            "location.hostname==="
            + json.dumps(route.hostname)
            + "&&location.pathname==="
            + json.dumps(route.path)
            + "&&!location.pathname.includes('login')&&document.readyState!=='loading'"
        )

    def response(self, record, data):
        path, scope, generation = record
        if generation != self.response_generation:
            return
        from .request_reuse import owner, rate_limited, enter_cooldown

        if isinstance(data, dict) and rate_limited(data.get('errorMsg') or data.get('error_msg')):
            enter_cooldown(owner(self.session), path)
        self.events.append((path, scope, data))
        self.events = self.events[-60:]
        self.changed.set()

    async def receive(self):
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                if not self.accept_message(message):
                    continue
                p = message.get("params", {})
                future = self.pending.pop(message.get("id"), None)
                if future and not future.done():
                    future.set_result(message)
                if message.get("method") == "Network.requestWillBeSent":
                    r = p["request"]
                    url = urlsplit(r["url"])
                    if url.hostname == "yingxiao.pinduoduo.com" and url.path in {LIST, ENTITY, LOGS} | SUMMARY_PATHS:
                        try:
                            body = json.loads(r.get("postData") or "{}")
                        except TypeError, ValueError:
                            continue
                        if not isinstance(body, dict):
                            continue
                        # Retain only matching fields, never page authentication/fingerprint data.
                        allowed = (
                            "beginDate",
                            "startDate",
                            "endDate",
                            "pageNumber",
                            "queryRange",
                            "scenesMode",
                            "queryDimensionType",
                            "endDayHour",
                            "plateType",
                        )
                        self.ad_requests[p["requestId"]] = (
                            url.path,
                            {k: body[k] for k in allowed if k in body},
                            self.response_generation,
                        )
                        self.note_request(url.path, self.ad_requests[p["requestId"]][1])
                if (
                    message.get("method") == "Network.responseReceived"
                    and p.get("response", {}).get("status", 200) >= 400
                ):
                    record = self.ad_requests.pop(p["requestId"], None)
                    if record:
                        self.response(
                            record,
                            {
                                "success": False,
                                "errorMsg": '平台提示请求频繁，请稍后重试' if p['response']['status'] == 429 else f"推广数据接口 HTTP {int(p['response']['status'])}，请检查登录和数据权限",
                            },
                        )
                if message.get("method") == "Network.loadingFailed":
                    record = self.ad_requests.pop(p["requestId"], None)
                    if record:
                        self.response(
                            record, {"success": False, "errorMsg": "推广数据请求中断，请检查网络或重新加载推广页"}
                        )
                if message.get("method") == "Network.loadingFinished" and p["requestId"] in self.ad_requests:
                    record = self.ad_requests.pop(p["requestId"])
                    task = asyncio.create_task(self.read_response(p["requestId"], record))
                    self.body_tasks.add(task)
                    task.add_done_callback(self.body_tasks.discard)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(PageReadError("推广页面连接已中断"))

    async def read_response(self, rid, record):
        try:
            data = json.loads((await self.call("Network.getResponseBody", {"requestId": rid}))["body"])
        except Exception:
            data = {"success": False, "errorMsg": "推广响应读取失败"}
        self.response(record, data)

    def matches(self, url, path):
        return url == path or path in SUMMARY_PATHS and url in SUMMARY_PATHS

    def pending_matching(self, path, options):
        """A request for exactly this range was sent and its body is still loading."""
        for url, scope, generation in self.ad_requests.values():
            if (
                generation == self.response_generation
                and self.matches(url, path)
                and request_date(scope.get("beginDate") or scope.get("startDate")) == options["date_from"]
                and request_date(scope.get("endDate")) == options["date_to"]
            ):
                return True
        return False

    @measured('query')
    async def wait(self, path, options, page_number=None, mode=None, plate=None, timeout=25, probe=8):
        started = time.monotonic()
        probed = False
        label = "推广汇总" if path in SUMMARY_PATHS else "推广列表"
        scope_text = f"{options['date_from']} 至 {options['date_to']}" + (
            f" 第 {page_number} 页" if page_number else ""
        )
        while True:
            for i, (url, scope, data) in enumerate(self.events):
                start = request_date(scope.get("beginDate") or scope.get("startDate"))
                end = request_date(scope.get("endDate"))
                number = scope.get("pageNumber") or scope.get("queryRange", {}).get("pageNumber")
                if (
                    self.matches(url, path)
                    and start == options["date_from"]
                    and end == options["date_to"]
                    and (page_number is None or str(page_number) == str(number))
                    and (mode is None or str(scope.get("scenesMode")) == str(mode))
                    and (plate is None or str(scope.get("plateType")) == str(plate))
                ):
                    self.events.pop(i)
                    return checked_result(data), scope
            elapsed = time.monotonic() - started
            if elapsed >= timeout:
                break
            if not probed and elapsed >= probe:
                probed = True
                # Early diagnosis instead of a blind wait: login lost, the range
                # option not selected, or the page queried another range.
                if not self.pending_matching(path, options):
                    state = await self.range_state(RANGES.get(options.get("trade_range"), ""))
                    if state.get("login"):
                        raise PageReadError("推广平台需要重新登录或验证（等待" + label + "时登录已失效）")
                    sent = [item for item in self.sent_scopes if item[3] >= started - 1 and self.matches(item[0], path)]
                    if state.get("found") and state.get("selected") is False and options.get("trade_range") in RANGES:
                        # The click did not take effect; select once more.
                        await self.select(RANGES[options["trade_range"]])
                    elif sent and all(
                        (item[1], item[2]) != (options["date_from"], options["date_to"]) for item in sent
                    ):
                        got = sent[-1]
                        raise PageReadError(
                            f"{label}请求日期 {got[1] or '未知'} 至 {got[2] or '未知'} 与指定范围 {scope_text} 不一致，"
                            "日期控件未切换到目标范围，保留上次数据"
                        )
            self.changed.clear()
            try:
                await asyncio.wait_for(
                    self.changed.wait(), max(0.05, min(timeout, probe if not probed else timeout) - elapsed)
                )
            except TimeoutError:
                pass
        sent = [item for item in self.sent_scopes if item[3] >= started - 1 and self.matches(item[0], path)]
        if self.pending_matching(path, options):
            reason = "已发出目标日期请求，平台响应较慢"
        elif any((item[1], item[2]) == (options["date_from"], options["date_to"]) for item in sent):
            reason = "已返回目标日期响应，但页码或场景与本次读取不一致"
        elif sent:
            reason = f"页面请求日期为 {sent[-1][1] or '未知'} 至 {sent[-1][2] or '未知'}，与目标范围不一致"
        else:
            reason = "页面未触发查询，请检查推广页日期控件、登录和权限"
        raise PageReadError(f"{label}未返回指定范围 {scope_text}：{reason}")

    async def select(self, label):
        return await self.evaluate(
            "(()=>{const e=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&e.textContent.trim()==="
            + json.dumps(label)
            + "&&e.offsetWidth>0);if(!e)return false;e.click();return true;})()"
        )

    @measured('page_load')
    async def open_promotion(self, kind, options):
        report(
            self,
            stage="复用推广页面并刷新本批数据"
            if getattr(self, "reused_kind", None) == kind
            else "打开推广页面，等待日期筛选",
        )
        await self.call("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Shanghai"})
        previous = None
        if getattr(self, "reused_kind", None) == kind:
            previous = await self.evaluate("performance.timeOrigin")
            await self.call("Page.reload", {"ignoreCache": False})
        else:
            if getattr(self, "document_guard", None):
                previous = await self.evaluate("performance.timeOrigin")
            await self.call("Page.navigate", {"url": URLS[kind]})
        for _ in range(60):
            fresh = "performance.timeOrigin!==" + json.dumps(previous) + "&&" if previous is not None else ""
            state = await self.evaluate(
                "({login:location.pathname.includes('login'),ready:"
                + fresh
                + "("
                + self.reuse_ready_expression(kind)
                + ")&&"
                + "[...document.querySelectorAll('*')].some(e=>e.children.length===0&&e.textContent.trim()==="
                + json.dumps(RANGES[options["trade_range"]])
                + "&&e.offsetWidth>0)})"
            )
            if state["ready"] and (not getattr(self, "document_guard", None) or self.document_guard.loader):
                break
            await asyncio.sleep(0.25)
        else:
            raise PageReadError(
                "推广平台需要重新登录或验证" if state.get("login") else "推广页面加载超时，请检查推广权限"
            )
        if not await self.select(RANGES[options["trade_range"]]):
            raise PageReadError("未找到推广日期筛选")


def request_date(value):
    """Compare the requested Beijing day, never accept a response for another range."""
    text = str(value or "")
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        if len(text) in (10, 13) and text.isdigit():
            return datetime.fromtimestamp(int(text) / (1000 if len(text) == 13 else 1), BEIJING).date().isoformat()
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (value.astimezone(BEIJING) if value.tzinfo else value).date().isoformat()
    except ValueError, OverflowError, OSError:
        return ""


async def read_rows(page, kind, options, mode=None):
    from . import collection_checkpoint as checkpoint

    if options["trade_range"] != "realtime" and checkpoint.current.get():
        return await historical_rows(page, kind, options, mode)
    path = LIST if kind == "ad_products" else ENTITY
    rows = []
    seen = set()
    total = 0
    complete = False
    for page_number in range(1, 201):
        data, _ = await page.wait(path, options, page_number, mode)
        batch = data.get("adInfos" if kind == "ad_products" else "entityReportList")
        total = data.get("totalAdNum" if kind == "ad_products" else "total")
        if batch is None and total == 0:
            batch = []
        if not isinstance(batch, list) or type(total) is not int or total < 0:
            raise ValueError("推广分页格式发生变化，保留上次数据")
        previous = len(rows)
        for raw in batch:
            row = product(raw, kind, mode, options)
            if row["id"] not in seen:
                rows.append(row)
                seen.add(row["id"])
        report(page, len(rows), total, "读取推广商品分页")
        if len(rows) >= total:
            complete = True
            break
        if len(rows) >= LIMIT or not batch or len(rows) == previous:
            break
        await asyncio.sleep(0.25)
        advanced = await page.evaluate(
            "(()=>{const e=[...document.querySelectorAll('.anq-pagination-next')].find(e=>e.offsetWidth>0);if(!e||e.classList.contains('anq-pagination-disabled'))return false;(e.querySelector('button')||e).click();return true;})()"
        )
        if not advanced:
            break
    return rows[:LIMIT], total, complete and len(rows) <= LIMIT


async def historical_rows(page, kind, options, mode):
    from . import collection_checkpoint as checkpoint
    from .native_pagination import move

    path = LIST if kind == "ad_products" else ENTITY
    position = 1
    last = None

    async def fetch(number):
        nonlocal position, last
        if number == position and last is not None:
            return last
        if number != position:
            await move(page, number, position, "promotion")
            position = number
        data, _ = await page.wait(path, options, number, mode)
        batch = data.get("adInfos" if kind == "ad_products" else "entityReportList")
        total = data.get("totalAdNum" if kind == "ad_products" else "total")
        if batch is None and total == 0:
            batch = []
        if not isinstance(batch, list) or type(total) is not int or total < 0:
            raise ValueError("推广分页格式发生变化，保留上次数据")
        rows = [product(raw, kind, mode, options) for raw in batch]
        last = {"rows": rows, "ids": [row["id"] for row in rows], "total": total}
        return last

    part = json.dumps(["promotion-history", kind, mode, options["date_from"], options["date_to"]], ensure_ascii=False)
    result = await checkpoint.paginate(
        part, fetch, lambda loaded, total: report(page, loaded, total, "读取历史推广分页"), restart_all=True
    )
    # IDs returned by a completed checkpoint must not be modified in-place by
    # the managed-list caller; they are durable part keys on the next batch.
    return [dict(row) for row in result["rows"]], result["source_total"], True


async def collect(session, kind, options):
    from .read_page_pool import borrow

    async with borrow(session, kind, PromotionPage) as page:
        await page.open_promotion(kind, options)
        report(page, stage="等待推广汇总响应")
        summary, scope = await page.wait(SUMMARY, options)
        metrics = fields(summary.get("sumReport"))
        if any(metrics[k] is None for k in ("ad_spend", "ad_revenue")):
            raise ValueError("推广概况未提供完整金额，保留上次数据")
        metrics.update(
            source_time=report_time(summary),
            date_from=options["date_from"],
            date_to=options["date_to"],
            through_hour=scope.get("endDayHour", scope.get("plateType")),
        )
        metrics["daily"] = [
            {"id": str(r["date"])[:10], "date": str(r["date"])[:10], **fields(r)}
            for r in summary.get("dailyReportList") or []
        ]
        metrics["hourly"] = [
            {"hour": r.get("hour"), "date": str(r.get("date", ""))[:10], **fields(r)}
            for r in summary.get("hourlyReportList") or []
        ]
        from .collection_checkpoint import current

        checkpoint = current.get()
        if options["trade_range"] != "realtime" and checkpoint:
            metrics = checkpoint.data.setdefault("promotion_summary", metrics)
        from .summary_publication import publish, only

        brief = {
            "rows": [], "metrics": metrics, "summary_complete": True, "complete": False, "source_unit": "推广汇总"
        }
        if only(kind, options):
            brief["details_on_demand"] = True
            return brief
        await publish(
            kind,
            options,
            brief,
        )
        report(page, stage="推广汇总已核验，读取商品明细")
        rows, total, complete = await read_rows(page, kind, options, 1 if kind == "ad_products" else None)
        if kind == "ad_products":
            if not await page.select("全店托管"):
                raise PageReadError("未找到全店托管列表入口")
            managed, count, done = await read_rows(page, kind, options, 3)
            for row in managed:
                row["id"] = "managed:" + row["id"]
            rows += managed
            total += count
            complete = complete and done
        return {
            "rows": rows,
            "metrics": metrics,
            "source_total": total,
            "source_unit": "推广记录",
            "complete": complete,
            "limit": None if options["trade_range"] != "realtime" and checkpoint else LIMIT,
            "source_scope": "稳定成本推广与全店托管；推广报表包含已删除推广的归因数据",
        }
