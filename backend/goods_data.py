"""Product analytics from the page's own read-only filters and pagination."""

from .collection_metrics import measured
import asyncio
import json
import time
from datetime import datetime, timedelta
from decimal import Decimal
from .transactions import number, options_for as range_options, wait_responses, RANGES
from .trade_font import display_map
from .product_images import thumbnail
from .sync_progress import report
from .business_reader import ReadPage, checked_result
from .read_errors import PageReadError

DETAIL = "queryGoodsDetailVOListForMMS"
HISTORY = "queryGoodsPageOverView"
LIVE = "queryGoodsPageOverviewForMms"
TREND = "queryGoodsPagePlnOstList"
DAY_TREND = "queryGoodsDataDayTrendForMms"
ENDPOINTS = {DETAIL, HISTORY, LIVE, TREND, DAY_TREND}
SUMMARY = {
    "visitors": "guv",
    "views": "gpv",
    "favorites": "goodsFavCnt",
    "visited_goods": "vstGoodsCnt",
    "sales": "payOrdrAmt",
    "orders": "payOrdrCnt",
    "buyers": "payOrdrUsrCnt",
    "conversion": "payUvRto",
}
FIELDS = {
    "visitors": "goodsUv",
    "views": "goodsPv",
    "sales": "payOrdrAmt",
    "quantity": "payOrdrGoodsQty",
    "orders": "payOrdrCnt",
    "buyers": "payOrdrUsrCnt",
    "conversion": "goodsVcr",
    "favorites": "goodsFavCnt",
    "order_rate": "ordrVstrRto",
    "payment_rate": "payOrdrRto",
    "traffic_loss": "imprUsrCnt",
}
SORT_FIELDS = set(FIELDS) | {"name"}


class GoodsPage(ReadPage):
    def __init__(self, session):
        super().__init__(session)
        self.detail_serial = 0
        self.detail_requests = {}
        self.detail_events = []
        self.detail_scope = None
        self.capture_detail = False

    def reset_buffers(self):
        super().reset_buffers()
        self.detail_requests.clear()
        self.detail_events.clear()
        self.detail_scope = None
        self.capture_detail = False

    def observe(self, message):
        if not self.capture_detail:
            return
        params = message.get("params", {})
        if message.get("method") == "Network.requestWillBeSent":
            request = params.get("request", {})
            if request.get("url", "").split("?")[0] == "https://mms.pinduoduo.com/sydney/api/goodsDataShow/" + DETAIL:
                try:
                    body = json.loads(request.get("postData") or "{}")
                except ValueError:
                    return
                if not isinstance(body, dict):
                    return
                self.detail_serial += 1
                scope = {
                    k: body.get(k)
                    for k in ("startDate", "endDate", "pageNum", "pageSize", "queryType", "sortCol", "sortType")
                }
                self.detail_requests[params["requestId"]] = (self.detail_serial, scope)
        if message.get("method") == "Network.loadingFinished" and params.get("requestId") in self.detail_requests:
            serial, scope = self.detail_requests.pop(params["requestId"])
            task = asyncio.create_task(self.read_detail(params["requestId"], serial, scope))
            self.body_tasks.add(task)
            task.add_done_callback(self.body_tasks.discard)

    async def read_detail(self, request_id, serial, scope):
        try:
            body = json.loads((await self.call("Network.getResponseBody", {"requestId": request_id}))["body"])
        except Exception:
            body = {"success": False, "errorMsg": "商品数据响应读取失败"}
        self.detail_events.append((serial, scope, body))
        self.detail_events = self.detail_events[-30:]

    @measured('query')
    async def wait_detail(self, options, number, after=-1):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            for index in range(len(self.detail_events) - 1, -1, -1):
                serial, scope, body = self.detail_events[index]
                actual = {k: v for k, v in scope.items() if k != "pageNum"}
                # The native history control determines the actual ready range.
                # Lock the first request made after that operation, rather than
                # assuming its dates from another summary endpoint.
                matched = (
                    self.detail_scope == actual
                    if self.detail_scope is not None
                    else number == 1
                    and all(actual.get(k) is not None for k in ("startDate", "endDate", "pageSize", "queryType"))
                )
                if serial > after and str(scope.get("pageNum")) == str(number) and matched:
                    self.detail_events.pop(index)
                    result = checked_result(body)
                    if self.detail_scope is None:
                        self.detail_scope = actual
                    return result
            await asyncio.sleep(0.2)
        raise PageReadError("历史商品数据未返回匹配日期和页码的响应，已保留断点")


def options_for(scope):
    return {**range_options(scope), "stars": []}


def normalize_row(item, mapping):
    if item.get("goodsId") is None:
        raise ValueError("商品数据缺少商品编号")
    row = {
        "id": str(item["goodsId"]),
        "goods_id": str(item["goodsId"]),
        "name": item.get("goodsName") or "",
        "thumbnail_url": thumbnail(item),
        "created_at": item.get("statDate") or "",
        **{k: number(item.get(v), mapping) for k, v in FIELDS.items()},
        "previous": {k: number(item.get(v + "Ytd"), mapping) for k, v in FIELDS.items()},
        "comparison": {
            k: {"value": number(item.get(v + "Ppr"), mapping), "percent": bool(item.get(v + "PprIsPercent"))}
            for k, v in FIELDS.items()
        },
        "conversion_level": {-1: "低", 0: "持平", 1: "高"}.get(item.get("goodsVcrRised"), "未提供"),
        "category_conversion": number(item.get("cate3AvgGoodsVcr"), mapping),
    }
    for key, field in FIELDS.items():
        comparison = row["comparison"][key]
        if comparison["percent"] and comparison["value"] is not None and "%" not in str(item.get(field + "Ppr", "")):
            comparison["value"] *= 100
    return row


def summary_fields(row, mapping, ratio=False):
    result = {key: number(row.get(field), mapping) for key, field in SUMMARY.items()}
    if ratio and result["conversion"] is not None:
        result["conversion"] *= 100
    return result


def normalize(raw, items, mapping, options, source_time, total, complete):
    scope = options["trade_range"]
    realtime = scope == "realtime"
    summary = summary_fields(raw[LIVE]["todayData"] if realtime else raw[HISTORY], mapping)
    if realtime:
        summary["favorites"] = number(raw[HISTORY].get("goodsFavCnt"), mapping)
    if summary["visitors"] is None or summary["sales"] is None:
        raise ValueError("商品概况字段不完整，请检查查看权限")
    day = datetime.fromisoformat(options["day"]).date()
    yesterday = day - timedelta(days=1)
    trend = []
    if realtime:
        series = [
            (raw[LIVE].get("todayList") or [], "今日累计", day.isoformat()),
            (raw[LIVE].get("yesList") or [], "昨日累计", yesterday.isoformat()),
        ]
    elif scope == "yesterday":
        series = [(raw.get(DAY_TREND, []), "昨日累计", yesterday.isoformat())]
    else:
        days = {"yesterday": 7, "week": 7, "month": 30}[scope]
        series = [
            (
                [
                    r
                    for r in raw.get(TREND, [])
                    if (yesterday - timedelta(days=days - 1)).isoformat()
                    <= r.get("statDate", "")
                    <= yesterday.isoformat()
                ],
                "每日",
                None,
            )
        ]
    for rows, label, date in series:
        for item in rows:
            trend.append(
                {
                    "id": label + ":" + (item.get("statDate") or date or "") + ":" + str(item.get("statHr")),
                    "series": label,
                    "date": item.get("statDate") or date,
                    "hour": item.get("statHr") or None,
                    **summary_fields(item, mapping, ratio=True),
                }
            )
    rows = [normalize_row(item, mapping) for item in items]
    return {
        "rows": rows,
        "source_total": total,
        "complete": complete,
        "source_unit": "商品",
        "metrics": {
            "summary": summary,
            "trend": trend,
            "range": scope,
            "source_time": source_time,
            "previous": summary_fields(raw[LIVE]["yesData"], mapping) if realtime else None,
        },
    }


async def click_period(page, label, index):
    # The product rows can also contain a "30日" new-product badge.
    clicked = await page.evaluate(
        "(()=>{const es=[...document.querySelectorAll('[class*=data-pick-group_dateItemStyle]')].filter(e=>e.offsetWidth>0&&e.textContent.trim()==="
        + json.dumps(label)
        + ");const e=es["
        + str(index)
        + "];if(e)e.click();return !!e})()"
    )
    if not clicked:
        raise ValueError("商品数据日期控件发生变化，请更新读取适配")


async def collect(page, options):
    detail_after = -1
    raw = await wait_responses(page, (LIVE, HISTORY, DETAIL))
    if options["trade_range"] != "realtime":
        names = (HISTORY, DAY_TREND if options["trade_range"] == "yesterday" else TREND)
        for name in names:
            page.trade_responses.pop(name, None)
        await click_period(page, RANGES[options["trade_range"]], 0)
        raw.update(await wait_responses(page, names))
        page.trade_responses.pop(DETAIL, None)
        page.capture_detail = True
        detail_after = getattr(page, "detail_serial", -1)
        await click_period(page, RANGES[options["trade_range"]], 1)
        raw.update(await wait_responses(page, (DETAIL,)))
    # Use the official 50-row option: changing live rank/ties across 10-row pages
    # can return a duplicate product at the boundary and omit another product.
    opened = await page.evaluate(
        "(()=>{const p=[...document.querySelectorAll('[data-testid=beast-core-pagination]')].find(e=>e.offsetWidth);if(!p||p.querySelector('input')?.value==='50')return false;const e=p.querySelector('[data-testid=beast-core-select-header]');e?.click();return !!e})()"
    )
    if opened:
        await asyncio.sleep(0.2)
        previous = page.trade_responses.pop(DETAIL, None)
        size_after = getattr(page, "detail_serial", -1)
        changed = await page.evaluate(
            "(()=>{const e=[...document.querySelectorAll('[role=option]')].find(e=>e.offsetWidth&&e.textContent.trim()==='50'&&e.getAttribute('data-disabled')!=='true');if(e){e.click();return true;}document.body.click();return false})()"
        )
        if changed:
            raw.update(await wait_responses(page, (DETAIL,)))
            detail_after = size_after
        elif previous is not None:
            page.trade_responses[DETAIL] = previous
    from . import collection_checkpoint as checkpoint

    if options["trade_range"] != "realtime" and checkpoint.current.get():
        raw[DETAIL] = await page.wait_detail(options, 1, detail_after)
        return await historical_rows(page, raw, options)
    batch = raw[DETAIL]
    items = []
    seen = set()
    total = 0
    while True:
        if not isinstance(batch, dict) or not isinstance(batch.get("goodsDetailList"), list) or "totalNum" not in batch:
            raise ValueError("商品明细字段发生变化，未保存不完整数据")
        total = int(batch["totalNum"])
        before = len(items)
        for item in batch["goodsDetailList"]:
            key = str(item.get("goodsId"))
            if key not in seen:
                items.append(item)
                seen.add(key)
        report(page, len(items), total, "读取商品指标分页")
        if len(items) >= total or len(items) >= 2000:
            break
        if len(items) == before:
            raise ValueError("商品数据翻页未前进，请重新读取")
        page.trade_responses.pop(DETAIL, None)
        clicked = await page.evaluate(
            "(()=>{const e=[...document.querySelectorAll('[data-testid=beast-core-pagination-next]')].find(e=>e.offsetWidth&&e.offsetHeight);if(!e||/disabled/.test(e.className)||e.getAttribute('aria-disabled')==='true'||e.getAttribute('data-disabled')==='true')return false;e.click();return true})()"
        )
        if not clicked:
            break
        batch = (await wait_responses(page, (DETAIL,)))[DETAIL]
        await asyncio.sleep(0.2)
    mapping = await display_map(page, [raw, items])
    source_time = await page.evaluate(
        "(()=>{const t=document.body.innerText.split('商品概况')[1]||'';return (t.match(/统计时间[：:]\\s*([^\\n]+)/)||[])[1]||null})()"
    )
    result = normalize(raw, items, mapping, options, source_time, total, len(items) >= total)
    if len(items) < total:
        result["scope_note"] = (
            f"平台总数 {total}，去重后读取 {len(items)}；实时排行跨页重复或平台限制导致缺口，当前按部分数据展示，后续周期重新核对。"
        )
    return result


async def historical_rows(page, raw, options):
    from . import collection_checkpoint as checkpoint
    from .native_pagination import move

    ctx = checkpoint.current.get()
    actual = dict(page.detail_scope)
    if ctx.data.get("goods_detail_scope") not in (None, actual):
        from .async_database import run as database_work

        await database_work(ctx.clear)
    ctx.data["goods_detail_scope"] = actual
    if "goods_summary" not in ctx.data:
        mapping = await display_map(page, raw)
        source_time = await page.evaluate(
            "(()=>{const t=document.body.innerText.split('商品概况')[1]||'';return (t.match(/统计时间[：:]\\s*([^\\n]+)/)||[])[1]||null})()"
        )
        ctx.data["goods_summary"] = normalize(raw, [], mapping, options, source_time, 0, False)["metrics"]
    position = 1
    initial = raw[DETAIL]
    last = None

    async def fetch(number):
        nonlocal position, initial, last
        if number == position and last is not None:
            return last
        if number == 1 and initial is not None:
            batch = initial
            initial = None
        else:
            page.trade_responses.pop(DETAIL, None)
            after = page.detail_serial
            await move(page, number, position, "goods")
            position = number
            batch = await page.wait_detail(options, number, after)
        if (
            not isinstance(batch, dict)
            or not isinstance(batch.get("goodsDetailList"), list)
            or type(batch.get("totalNum")) is not int
            or batch["totalNum"] < 0
        ):
            raise ValueError("商品明细字段发生变化，未保存不完整数据")
        # Persist decoded values, not font-obfuscated digits from a previous
        # browser page. A resumed page may use a different numeric font.
        mapping = await display_map(page, batch["goodsDetailList"])
        rows = [normalize_row(item, mapping) for item in batch["goodsDetailList"]]
        last = {"rows": rows, "ids": [row["id"] for row in rows], "total": batch["totalNum"]}
        return last

    part = json.dumps(
        ["goods-history", options["trade_range"], options["day"], actual], ensure_ascii=False, sort_keys=True
    )
    result = await checkpoint.paginate(
        part, fetch, lambda loaded, total: report(page, loaded, total, "读取历史商品指标分页"), restart_all=True
    )
    return {**result, "source_unit": "商品", "metrics": ctx.data["goods_summary"]}


def summarize(metrics):
    totals = {}
    for key in SUMMARY:
        if key == "conversion":
            continue
        values = [Decimal(str(row["summary"][key])) for row in metrics if row.get("summary", {}).get(key) is not None]
        totals[key] = float(sum(values)) if values else None
    return totals


def sort_rows(rows, key, direction):
    if key not in SORT_FIELDS or direction not in ("asc", "desc"):
        raise ValueError("商品数据排序条件无效")
    present = [r for r in rows if r.get(key) is not None]
    missing = [r for r in rows if r.get(key) is None]
    return sorted(present, key=lambda r: r[key], reverse=direction == "desc") + missing
