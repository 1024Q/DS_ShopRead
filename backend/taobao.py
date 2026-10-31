"""Read the authenticated Shengyicanmou UI and its own responses. No replayed APIs."""

import asyncio
import base64
import json
import math
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from .business_reader import ReadPage, BEIJING
from .read_errors import PageReadError
from .sync_progress import report

PAGES = {
    "overview": "https://sycm.taobao.com/portal/home.htm",
    "transactions": "https://sycm.taobao.com/portal/home.htm",
    "goodsdata": "https://sycm.taobao.com/cc/item_rank",
    "packages": "https://myseller.taobao.com/home.htm/package-center/packageMonitor",
    "aftersales": "https://myseller.taobao.com/home.htm/package-center/packageMonitor",
    "invoices": "https://myseller.taobao.com/home.htm/merchant-invoice/",
    "ad_products": "https://one.alimama.com/index.html#!/manage/onesite",
    "ad_reports": "https://one.alimama.com/index.html#!/report/account?rptType=account",
}
LIVE = "/portal/live/new/index/"
HISTORY = "/portal/coreIndex/new/"
CUSTOM = "/portal/long/period/nodistinct/coreIndex/"
ITEMS = "/cc/item/view/top.json"
IDENTITY = "/custom/menu/getPersonalView.json"
PATHS = {
    LIVE + "overview/v3.json",
    LIVE + "trend/v3.json",
    HISTORY + "overview/v3.json",
    HISTORY + "trend/v3.json",
    CUSTOM + "getShopMainIndexes/v3.json",
    CUSTOM + "new/trend/v3.json",
    ITEMS,
    IDENTITY,
}
FIELDS = {
    "sales": "payAmt",
    "orders": "payOrdCnt",
    "buyers": "payByrCnt",
    "visitors": "uv",
    "views": "pv",
    "conversion": "payRate",
    "average": "payPct",
    "refund_amount": "rfdSucAmt",
    "net_payment": "netPaymentAmount",
    "quantity": "payItmCnt",
    "cart_buyers": "cartByrCnt",
    "cart_quantity": "cartItemCnt",
}
GOODS = {
    "sales": "payAmt",
    "quantity": "payItmCnt",
    "buyers": "payByrCnt",
    "visitors": "itmUv",
    "views": "itmPv",
    "conversion": "payRate",
    "favorites": "itemCltByrCnt",
    "refund_amount": "sucRefundAmt",
    "cart_quantity": "itemCartCnt",
}
NOTE = (
    "淘宝生意参谋：成交金额为支付金额，订单数为支付子订单数，访客数为店铺访客；退款按完结时间统计。小时趋势为累计值。"
)
GOODS_NOTE = (
    "淘宝生意参谋商品排行，含小主图；不是商品库存目录，不用于上新统计。未提供店铺去重概况的指标显示 —；单品人数不相加。"
)


def number(value):
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or value in ("", "-", "--"):
        return None
    if isinstance(value, bool):
        raise ValueError("生意参谋数值格式发生变化")
    try:
        result = float(value)
    except ValueError, TypeError:
        raise ValueError("生意参谋数值格式发生变化") from None
    if not math.isfinite(result):
        raise ValueError("生意参谋返回无效数值")
    return result


def fields(raw, mapping=FIELDS):
    result = {key: number(raw.get(source)) for key, source in mapping.items()}
    if result.get("conversion") is not None:
        result["conversion"] *= 100
    return result


def checked(body, allow_list=False):
    if not isinstance(body, dict) or body.get("hasError"):
        raise ValueError("生意参谋返回错误，请在官方页面检查数据权限")
    envelope = body.get("content", body)
    if (
        not isinstance(envelope, dict)
        or envelope.get("code") not in (0, "0")
        or not isinstance(envelope.get("data"), (dict, list) if allow_list else dict)
    ):
        raise ValueError("生意参谋数据未就绪或无查看权限，保留原记录")
    return envelope["data"]


def period(options):
    day = datetime.fromisoformat(options["day"]).date()
    scope = options.get("trade_range", "realtime")
    if scope == "realtime":
        return day.isoformat(), day.isoformat()
    if scope not in ("yesterday", "week", "month"):
        raise ValueError("淘宝统计范围无效")
    end = day - timedelta(days=1)
    return (end - timedelta(days={"yesterday": 1, "week": 7, "month": 30}[scope] - 1)).isoformat(), end.isoformat()


def matches(query, options):
    first, last = period(options)
    return (query.get("dateRange") or [""])[0].split(",")[0] == first + "|" + last


def normalize_trade(overview, trend, options, source_time):
    live = options.get("trade_range", "realtime") == "realtime"
    if live:
        source_time = overview.get("updateTime")
        if not source_time or source_time[:10] != options["day"]:
            raise ValueError("生意参谋实时数据日期不匹配，等待下一轮采集")
        core = overview.get("data", {})
        current = core.get("today")
        previous = core.get("yestday")
    else:
        current = overview.get("self", overview)
        previous = None
    if not isinstance(current, dict):
        raise ValueError("生意参谋交易概况字段不完整")
    summary = {
        **{key: None for key in ("returning_rate", "followers", "refund_orders", "visitor_value")},
        **fields(current),
    }
    if any(summary[key] is None for key in ("sales", "orders", "buyers")):
        raise ValueError("生意参谋未返回完整支付指标，保留原记录")
    # Some historical pages call the same completed refund metric sucRefundAmt.
    if summary["refund_amount"] is None:
        summary["refund_amount"] = number(current.get("sucRefundAmt"))
    rows = []
    first, last = period(options)
    yesterday = (datetime.fromisoformat(options["day"]) - timedelta(days=1)).date().isoformat()
    sequences = (
        [
            (trend.get("data", {}).get("today", {}), "今日累计", options["day"]),
            (trend.get("data", {}).get("yesterday", {}), "昨日累计", yesterday),
        ]
        if live
        else [(trend.get("self", {}), "每日", None)]
    )
    for sequence, label, date in sequences:
        slots = sequence.get("statHour" if live else "statDate", [])
        if not isinstance(slots, list):
            raise ValueError("生意参谋趋势格式发生变化")
        for index, slot in enumerate(slots):
            row_date = date if live else datetime.fromtimestamp(float(slot) / 1000, BEIJING).date().isoformat()
            if not live and not first <= row_date <= last:
                continue
            hour = int(slot) if live else None
            if live and (not 0 <= hour <= 23 or (label == "今日累计" and hour > int(source_time[11:13]))):
                continue
            raw = {
                key: values[index]
                for key, values in sequence.items()
                if isinstance(values, list) and index < len(values)
            }
            rows.append(
                {
                    "id": f"{label}:{row_date}:{hour}",
                    "name": label,
                    "series": label,
                    "date": row_date,
                    "hour": hour,
                    "created_at": row_date,
                    **fields(raw),
                }
            )
    if not rows:
        raise ValueError("生意参谋未返回所选日期的趋势，保留原记录")
    comparisons = {}
    for key, source in FIELDS.items():
        if live:
            value = fields(previous or {}).get(key)
            comparisons[key] = {"value": value, "percent": False, "rising": False, "absolute": True}
        else:
            change = number(current.get(source, {}).get("cycleCrc")) if isinstance(current.get(source), dict) else None
            comparisons[key] = {
                "value": abs(change * 100) if change is not None else None,
                "percent": True,
                "rising": change is not None and change >= 0,
            }
    note = NOTE + (
        " 自定义多日人数按平台原始未去重口径展示。" if options.get("trade_range") in ("week", "month") else ""
    )
    return {
        "rows": rows,
        "source_total": len(rows),
        "complete": True,
        "source_unit": "时段 / 日期",
        "source_name": "淘宝生意参谋",
        "scope_note": note,
        "metrics": {
            "summary": summary,
            "comparisons": comparisons,
            "trend": rows,
            "annual": [],
            "regions": [],
            "orders_info": {},
            "lead": {},
            "range": options.get("trade_range", "realtime"),
            "source_time": source_time,
            "realtime": live,
        },
    }


def normalize_goods(items, total, options, source_time):
    rows = []
    _, last = period(options)
    for raw in items:
        item = raw.get("item") or {}
        goods_id = str(item.get("itemId") or "")
        if not goods_id.isdigit():
            raise ValueError("生意参谋商品缺少有效商品 ID")
        url = item.get("pictUrl") or ""
        if url.startswith("//"):
            url = "https:" + url
        if urlsplit(url).scheme != "https":
            url = ""
        values = fields(raw, GOODS)
        rows.append(
            {
                "id": goods_id,
                "goods_id": goods_id,
                "name": item.get("title") or "",
                "thumbnail_url": url,
                "created_at": last,
                "status": raw.get("itemStatus") or "",
                **values,
                "orders": None,
                "order_rate": None,
                "payment_rate": None,
                "traffic_loss": None,
                "previous": {},
                "comparison": {
                    key: {
                        "value": number(raw.get(field, {}).get("cycleCrc")) * 100
                        if isinstance(raw.get(field), dict) and number(raw[field].get("cycleCrc")) is not None
                        else None,
                        "percent": True,
                    }
                    for key, field in GOODS.items()
                },
            }
        )
    return {
        "rows": rows,
        "source_total": total,
        "complete": len(rows) >= total,
        "source_unit": "商品",
        "source_name": "淘宝生意参谋",
        "scope_note": GOODS_NOTE,
        "metrics": {
            "summary": {
                key: None
                for key in (
                    "visitors",
                    "views",
                    "favorites",
                    "visited_goods",
                    "sales",
                    "orders",
                    "buyers",
                    "conversion",
                )
            },
            "trend": [],
            "previous": None,
            "range": options["trade_range"],
            "source_time": source_time,
        },
    }


class SycmPage(ReadPage):
    def __init__(self, session):
        super().__init__(session)
        self.sycm_requests = {}
        self.responses = []

    def observe(self, message):
        from .taobao_flow import PATHS as FLOW_PATHS

        params = message.get("params", {})
        method = message.get("method")
        if method == "Network.responseReceived":
            url = urlsplit(params["response"]["url"])
            if url.scheme == "https" and url.hostname == "sycm.taobao.com" and url.path in PATHS | FLOW_PATHS:
                self.sycm_requests[params["requestId"]] = (url.path, parse_qs(url.query))
        if method == "Network.loadingFinished" and params["requestId"] in self.sycm_requests:
            task = asyncio.create_task(self.read_sycm(params["requestId"]))
            self.body_tasks.add(task)
            task.add_done_callback(self.body_tasks.discard)

    async def read_sycm(self, request_id):
        path, query = self.sycm_requests.pop(request_id)
        try:
            response = await self.call("Network.getResponseBody", {"requestId": request_id})
            text = base64.b64decode(response["body"]).decode() if response.get("base64Encoded") else response["body"]
            self.responses.append((path, query, json.loads(text)))
        except ValueError, KeyError:
            self.responses.append((path, query, {}))

    async def wait_data(self, paths, options=None, page_number=None, timeout=22, allow_list=False):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for path, query, body in reversed(self.responses):
                if path not in paths:
                    continue
                if options and not matches(query, options):
                    continue
                if page_number is not None and (query.get("page") or ["1"])[0] != str(page_number):
                    continue
                return checked(body, allow_list)
            await asyncio.sleep(0.2)
        snapshot = await self.evaluate("({url:location.href,text:document.body?.innerText?.slice(0,2500)||''})")
        if urlsplit(snapshot["url"]).hostname != "sycm.taobao.com" or any(
            t in snapshot["text"] for t in ("密码登录", "扫码登录", "请登录")
        ):
            raise PageReadError("生意参谋登录已失效，请在店铺管理完成登录")
        if any(t in snapshot["text"] for t in ("安全验证", "滑动验证", "完成验证")):
            raise PageReadError("生意参谋需要人工验证，请打开官方页面完成验证")
        raise ValueError("生意参谋未返回该范围数据；请检查官方页加载状态和查看权限，原记录已保留")

    async def click_label(self, label):
        for _ in range(40):
            clicked = await self.evaluate(
                "(()=>{const e=[...document.querySelectorAll('*')].find(e=>e.offsetWidth>0&&e.children.length===0&&e.textContent.replace(/\\s/g,'')==="
                + json.dumps(label)
                + ");e?.click();return !!e})()"
            )
            if clicked:
                return
            await asyncio.sleep(0.2)
        raise ValueError("生意参谋日期筛选入口发生变化")

    async def select_custom(self, options):
        opened = await self.evaluate(
            "(()=>{const e=document.querySelector('.common-filter-custom-options');e?.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));return !!e})()"
        )
        if not opened:
            raise ValueError("生意参谋自定义日期入口发生变化")
        await asyncio.sleep(0.3)
        for index, date in enumerate(period(options)):
            for attempt in range(4):
                found = await self.evaluate(
                    "(()=>{const es=[...document.querySelectorAll('[data-role=date][data-value=\""
                    + date
                    + "\"]')].filter(e=>e.offsetWidth&&!e.className.includes('disabled'));const e=es["
                    + ("es.length-1" if index else "0")
                    + "];e?.click();return !!e})()"
                )
                if found:
                    break
                direction = "next-month" if index else "prev-month"
                moved = await self.evaluate(
                    "(()=>{const es=[...document.querySelectorAll('[data-role="
                    + direction
                    + "]')].filter(e=>e.offsetWidth&&!e.className.includes('disabled'));const e=es["
                    + ("es.length-1" if index else "0")
                    + "];e?.click();return !!e})()"
                )
                if not moved:
                    raise ValueError("生意参谋无法选择所需日期，未保存其他周期数据")
                await asyncio.sleep(0.15)
            else:
                raise ValueError("生意参谋日期超出可选范围")
            await asyncio.sleep(0.2)
        await self.click_label("确定")

    async def identity(self):
        raw = await self.wait_data({IDENTITY})
        title = raw.get("runAsShopTitle")
        shop_id = raw.get("runAsShopId")
        if not title or not shop_id:
            raise ValueError("生意参谋未返回当前店铺身份，未保存数据")
        expected = self.session.shop.get("detected_name")
        if expected and title.strip() != expected.strip():
            raise ValueError("生意参谋店铺与已登录淘宝店铺不一致，请在官方后台确认切店状态")
        return str(shop_id)


async def collect(session, kind, options):
    if kind not in PAGES:
        raise ValueError("淘宝此模块尚未接入")
    if kind in ("packages", "aftersales", "invoices"):
        from .taobao_operations import collect as read_operations

        return await read_operations(session, kind, options)
    if kind in ("ad_products", "ad_reports"):
        from .taobao_ads import collect as read_ads

        return await read_ads(session, kind, options)
    options = {
        **options,
        "trade_range": options.get("trade_range") or "realtime",
        "day": options.get("day") or datetime.now(BEIJING).date().isoformat(),
    }
    async with SycmPage(session) as page:
        await page.call("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Shanghai"})
        await page.call("Page.navigate", {"url": PAGES[kind]})
        shop_id = await page.identity()
        scope = options["trade_range"]
        if kind == "goodsdata":
            ranking_error = None
            if scope == "realtime":
                try:
                    await page.wait_data({ITEMS}, options, timeout=12)
                except PageReadError:
                    raise
                except ValueError:
                    ranking_error = (
                        "官方实时商品排行未返回，当前显示实时流量看板；商品排行请切换昨日、近 7 日或近 30 日。"
                    )
            else:
                await page.click_label({"yesterday": "日", "week": "7天", "month": "30天"}[scope])
            items = []
            seen = set()
            total = None
            for number_ in range(1, 201):
                if ranking_error:
                    total = 0
                    break
                batch = await page.wait_data({ITEMS}, options, page_number=number_)
                if not isinstance(batch.get("data"), list) or type(batch.get("recordCount")) is not int:
                    raise ValueError("生意参谋商品分页格式发生变化")
                total = batch["recordCount"]
                before = len(items)
                for item in batch["data"]:
                    key = str(item.get("item", {}).get("itemId") or "")
                    if key not in seen:
                        items.append(item)
                        seen.add(key)
                report(page, len(items), total, "读取淘宝商品排行")
                if len(items) >= total or len(items) >= 2000:
                    break
                if len(items) == before:
                    raise ValueError("生意参谋商品翻页未前进，保留原记录")
                clicked = await page.evaluate(
                    "(()=>{const e=document.querySelector('li.ant-pagination-next');if(!e||e.getAttribute('aria-disabled')==='true')return false;e.click();return true})()"
                )
                if not clicked:
                    raise ValueError("生意参谋商品分页未完成，保留原记录")
            result = normalize_goods(items, total, options, period(options)[1])
            from .taobao_flow import collect as collect_flow

            result["metrics"]["traffic"] = await collect_flow(page, options)
            result["metrics"]["source_time"] = result["metrics"]["traffic"]["source_time"]
            result["scope_note"] += " 流量看板按平台独立口径展示。"
            if ranking_error:
                result["complete"] = False
                result["source_total"] = None
                result["ranking_error"] = ranking_error
                result["metrics"]["ranking_error"] = ranking_error
        else:
            live = await page.wait_data({LIVE + "overview/v3.json"})
            if scope == "realtime":
                overview = live
                trend = await page.wait_data({LIVE + "trend/v3.json"})
                source_time = overview.get("updateTime")
            else:
                if scope == "yesterday":
                    await page.click_label("日")
                else:
                    await page.select_custom(options)
                overview = await page.wait_data(
                    {HISTORY + "overview/v3.json", CUSTOM + "getShopMainIndexes/v3.json"}, options
                )
                trend = await page.wait_data({HISTORY + "trend/v3.json", CUSTOM + "new/trend/v3.json"}, options)
                source_time = period(options)[0] + " 至 " + period(options)[1]
            result = normalize_trade(overview, trend, options, source_time)
            if kind == "overview":
                result["metrics"] = {**result["metrics"]["summary"], "source_time": result["metrics"]["source_time"]}
                result["rows"] = []
                result["source_total"] = 1
        result["source_shop_id"] = shop_id
        return result
