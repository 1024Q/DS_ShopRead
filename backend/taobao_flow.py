"""Shengyicanmou traffic board: shop/item traffic, hourly/daily data and Top 10."""

from datetime import datetime, timedelta
from .taobao import number, period

URL = "https://sycm.taobao.com/flow/monitor/overview"
LIVE = "/flow/new/live/guide/trend/overview.json"
LIVE_TREND = "/flow/new/live/guide/trend.json"
SUMMARY = "/flow/new/guide/trend/overview.json"
TREND = "/flow/new/guide/trend.json"
CHANNELS = "/flow/v3/overview/shopFlowSourceTop/v4.json"
ITEMS = "/flow/new/monitor/item/tops/rank.json"
PATHS = {LIVE, LIVE_TREND, SUMMARY, TREND, CHANNELS, ITEMS}
FIELDS = {
    "shop_visitors": "uv",
    "item_visitors": "itmUv",
    "shop_views": "pv",
    "item_views": "itmPv",
    "buyers": "payByrCnt",
    "sales": "payAmt",
    "conversion": "payRate",
    "new_visitors": "newUv",
    "returning_visitors": "oldUv",
    "cart_buyers": "cartByrCnt",
    "favorites": "itmCltByrCnt",
}


def values(raw):
    result = {key: number(raw.get(field)) for key, field in FIELDS.items()}
    if result["conversion"] is not None:
        result["conversion"] *= 100
    return result


def normalize(summary, trend, channels, items, options):
    live = options["trade_range"] == "realtime"
    first, last = period(options)
    raw = summary.get("data", {}) if live else summary
    source_time = summary.get("updateTime") if live else first + " 至 " + last
    if live and (not source_time or source_time[:10] != options["day"]):
        raise ValueError("流量看板实时日期不匹配")
    metrics = values(raw)
    if metrics["shop_visitors"] is None or metrics["item_visitors"] is None:
        raise ValueError("流量看板未返回店铺或商品访客，保留原记录")
    rows = []
    end = datetime.fromisoformat(last).date()
    series = (
        [
            (trend.get("data", {}).get("today", {}), "今日分时", options["day"]),
            (trend.get("data", {}).get("yesterday", {}), "昨日分时", (end - timedelta(days=1)).isoformat()),
        ]
        if live
        else [(trend.get("my", {}), "每日", None)]
    )
    for data, label, date in series:
        slots = (
            range(max((len(v) for v in data.values() if isinstance(v, list)), default=0))
            if live
            else data.get("statDate", [])
        )
        for index, slot in enumerate(slots):
            if live:
                if index > 23 or (label == "今日分时" and index > int(source_time[11:13])):
                    continue
                stat = date
                hour = index
            else:
                stat = str(slot)
                if len(stat) == 5:
                    stat = f"{end.year}-{stat}"
                    if stat > last:
                        stat = f"{end.year - 1}-{str(slot)}"
                if not first <= stat <= last:
                    continue
                hour = None
            row = {k: v[index] for k, v in data.items() if isinstance(v, list) and index < len(v)}
            rows.append({"id": f"{label}:{stat}:{hour}", "series": label, "date": stat, "hour": hour, **values(row)})

    def scalar(item, key):
        value = item.get(key)
        return value.get("value") if isinstance(value, dict) else value

    sources = []
    for item in channels or []:
        sources.append(
            {
                "id": str(scalar(item, "pageId") or len(sources)),
                "name": scalar(item, "pageName") or "",
                "visitors": number(item.get("uv")),
                "buyers": number(item.get("itmPayByrCnt")),
                "conversion": number(item.get("payRate")) * 100 if number(item.get("payRate")) is not None else None,
            }
        )
    goods = []
    for row in (items or {}).get("data", []):
        item = row.get("item") or {}
        url = item.get("pictUrl") or ""
        goods.append(
            {
                "goods_id": str(item.get("itemId") or scalar(row, "itemId") or ""),
                "name": item.get("title") or "",
                "thumbnail_url": "https:" + url if url.startswith("//") else url if url.startswith("https://") else "",
                "visitors": number(row.get("uv")),
                "buyers": number(row.get("payOrderByrCnt")),
                "conversion": number(row.get("payRate")) * 100 if number(row.get("payRate")) is not None else None,
            }
        )
    return {
        "summary": metrics,
        "trend": rows,
        "channels": sources,
        "items": goods,
        "range": options["trade_range"],
        "source_time": source_time,
        "note": "流量总览为所有终端，来源与商品来源榜为无线端 Top 10。分时访客按小时去重，不跨小时相加；各渠道访客可能重叠。",
        "ranking_available": not live,
    }


async def collect(page, options):
    await page.call("Page.navigate", {"url": URL})
    await page.identity()
    scope = options["trade_range"]
    summary = await page.wait_data({LIVE})
    trend = await page.wait_data({LIVE_TREND})
    channels = []
    items = {}
    ranking_note = ""
    if scope != "realtime":
        await page.click_label({"yesterday": "日", "week": "7天", "month": "30天"}[scope])
        summary = await page.wait_data({SUMMARY}, options)
        trend = await page.wait_data({TREND}, options)
        # The channel/item panels mount only after entering the viewport.
        await page.evaluate(
            "(()=>{const e=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&e.textContent.replace(/\\s/g,'')==='流量来源排行Top10');e?.scrollIntoView({block:'center'})})()"
        )
        try:
            channels = await page.wait_data({CHANNELS}, options, allow_list=True, timeout=4)
            await page.evaluate(
                "(()=>{const e=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&e.textContent.replace(/\\s/g,'')==='商品来源排行Top10');e?.scrollIntoView({block:'center'})})()"
            )
            items = await page.wait_data({ITEMS}, options, timeout=4)
        except ValueError:
            ranking_note = "流量来源与商品来源 Top 10 本次未返回可核验响应；流量概况、趋势及独立商品排行已读取。"
    result = normalize(summary, trend, channels, items, options)
    if ranking_note:
        result.update(ranking_available=False, ranking_note=ranking_note)
    return result
