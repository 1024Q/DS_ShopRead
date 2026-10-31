"""Read only responses produced by the merchant transaction page controls."""

import asyncio
import json
import math
from datetime import datetime, timedelta
from decimal import Decimal
from .business_reader import BEIJING, checked_result
from .read_errors import PageReadError
from .trade_font import display_map
from .sync_progress import report

ENDPOINTS = {
    "getMallTradeInfo",
    "queryMallTradeList",
    "queryMallConfigurationList",
    "getMallNotPayOrderInfoV2",
    "getLeadPayOrderInfo",
    "queryMallGeographyDistributionList",
}
CORE_ENDPOINTS = {"getMallTradeInfo", "queryMallTradeList"}
ENDPOINT_LABELS = {
    "getMallTradeInfo": "交易概况",
    "queryMallTradeList": "成交趋势",
    "queryMallConfigurationList": "年度统计",
    "getMallNotPayOrderInfoV2": "待付款订单",
    "getLeadPayOrderInfo": "引导支付",
    "queryMallGeographyDistributionList": "地域分布",
}
RANGES = {"realtime": "实时", "yesterday": "昨日", "week": "7日", "month": "30日"}
FIELDS = {
    "sales": "payOrdrAmt",
    "orders": "payOrdrCnt",
    "buyers": "payOrdrUsrCnt",
    "average": "payOrdrAup",
    "conversion": "payUvRto",
    "returning_rate": "rpayUsrRtoDth",
    "followers": "mallFavCnt",
    "refund_amount": "sucRfOrdrAmt1d",
    "refund_orders": "sucRfOrdrCnt1d",
    "visitor_value": "uvCfmVal",
}


def options_for(trade_range):
    if trade_range not in RANGES:
        raise ValueError("交易统计范围无效")
    return {"trade_range": trade_range, "day": datetime.now(BEIJING).date().isoformat()}


def number(value, mapping=None):
    if value is None or value in ("", "--", "—", "-"):
        return None
    if isinstance(value, bool):
        raise ValueError("交易数据数字字段发生变化")
    text = str(value).translate(mapping or {}).replace(",", "").replace("%", "").strip()
    try:
        result = float(text)
    except ValueError as error:
        raise ValueError("交易数据包含无法识别的数字，未保存本次结果") from error
    if not math.isfinite(result):
        raise ValueError("交易数据包含无效数字")
    return result


def cents(value):
    return float(Decimal(str(value)) / 100) if value is not None else None


def fields(row, mapping=None, ratios=False):
    result = {key: number(row.get(source), mapping) for key, source in FIELDS.items()}
    if ratios:
        for key in ("conversion", "returning_rate"):
            if result[key] is not None:
                result[key] *= 100
    return result


def normalize(raw, mapping, options, source_time):
    summary = fields(raw["getMallTradeInfo"], mapping)
    if any(summary[key] is None for key in ("sales", "orders", "buyers")):
        raise ValueError("平台未提供完整交易概况，请检查店铺数据权限")
    trade = raw["queryMallTradeList"]
    day = options["day"]
    rows = []
    yesterday = (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat()
    for key, label, date in [
        ("todayRtList", "今日累计", day),
        ("yesterdayRtList", "昨日累计", yesterday),
        ("todayPerHourRtList", "今日分时", day),
        ("yesterdayPerHourRtList", "昨日分时", yesterday),
        ("dayList", "每日", None),
    ]:
        for item in trade.get(key) or []:
            date_text = item.get("stateDate") or date
            if not date_text:
                raise ValueError("交易趋势缺少统计日期")
            hour = item.get("hr")
            rows.append(
                {
                    "id": key + ":" + date_text + ":" + str(hour),
                    "series": label,
                    "date": date_text,
                    "hour": hour,
                    "created_at": date_text,
                    "name": label,
                    **fields(item, ratios=True),
                }
            )
    annual = raw["queryMallConfigurationList"]
    history = {item["month"]: item for item in annual.get("historyDataVOList") or []}
    current = {item["month"]: item for item in annual.get("currentDataVOList") or []}
    targets = {item["month"]: item for item in annual.get("manageConfigureVOList") or []}
    annual_rows = []
    for month in sorted(set(targets) | set(current) | set(history)):
        item = targets.get(month, {})
        year = item.get("year") or current.get(month, {}).get("year") or int(day[:4])
        annual_rows.append(
            {
                "month": month,
                "year": year,
                "previous_year": history.get(month, {}).get("year", year - 1),
                "sales": number(current.get(month, {}).get("payOrdrAmt"), mapping),
                "previous_sales": number(history.get(month, {}).get("payOrdrAmt"), mapping),
                "target": number(item.get("payOrdrAmt"), mapping),
                "completion": number(item.get("percent"), mapping),
            }
        )
    order = raw["getMallNotPayOrderInfoV2"]
    lead = raw["getLeadPayOrderInfo"]
    regions = []
    for item in raw["queryMallGeographyDistributionList"].get("geographyDistributionVOList") or []:
        # Displayed yuan fields retain precision; *_Cent may be truncated.
        regions.append(
            {
                "id": str(item["provinceId"]),
                "name": item.get("provinceName"),
                "date": item.get("statDate"),
                "sales": number(item.get("cfmOrdrAmt")),
                "orders": number(item.get("cfmOrdrCnt")),
                "buyers": number(item.get("cfmOrdrUsrCnt")),
                "order_average": number(item.get("cfmOrdrAop")),
                "buyer_average": number(item.get("cfmOrdrAup")),
            }
        )
    comparisons = {
        key: {
            "value": number(raw["getMallTradeInfo"].get(source + "Pct"), mapping),
            "percent": bool(raw["getMallTradeInfo"].get(source + "IsPercent")),
            "rising": bool(raw["getMallTradeInfo"].get(source + "PctRised")),
        }
        for key, source in FIELDS.items()
    }
    return {
        "rows": rows,
        "source_total": len(rows),
        "source_unit": "时段 / 日期",
        "complete": True,
        "metrics": {
            "summary": summary,
            "comparisons": comparisons,
            "trend": rows,
            "annual": annual_rows,
            "regions": regions,
            "range": options["trade_range"],
            "source_time": source_time,
            "realtime": options["trade_range"] == "realtime",
            "orders_info": {
                "time": order.get("readyDate"),
                "pending_orders": number(order.get("notPayOrderCnt")),
                "pending_amount": cents(order.get("notPayOrderAmountCnt")),
                "settlement_amount": cents(order.get("settlementShippingAmount")),
            },
            "lead": {
                "through": lead.get("readyDate"),
                "buyers": number(lead.get("allLeadPayPplCnt1m")),
                "sales": cents(lead.get("allLeadPayOrdrAmt1m")),
            },
        },
    }


async def wait_responses(page, names):
    deadline = asyncio.get_running_loop().time() + 20
    while True:
        result = {name: checked_result(page.trade_responses[name]) for name in names if name in page.trade_responses}
        if len(result) == len(names):
            return result
        if await page.evaluate("location.href.includes('/login')"):
            raise PageReadError("交易数据页面登录已失效")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        changed = getattr(page, "trade_changed", None)
        if changed is None:
            await asyncio.sleep(min(0.2, remaining))
        else:
            changed.clear()
            # A response may arrive while the login check above is in flight.
            if any(name in page.trade_responses and name not in result for name in names):
                continue
            try:
                await asyncio.wait_for(changed.wait(), min(1, remaining))
            except TimeoutError:
                pass
    missing = "、".join(ENDPOINT_LABELS.get(name, name) for name in sorted(names) if name not in page.trade_responses)
    raise PageReadError("交易数据未加载完成：" + missing + "，请检查数据查看权限")


async def click_range(page, label, last=False):
    clicked = await page.evaluate(
        "(()=>{const es=[...document.querySelectorAll('*')].filter(e=>e.children.length===0&&e.offsetWidth>0&&e.textContent.trim()==="
        + json.dumps(label)
        + ");const e=es["
        + ("es.length-1" if last else "0")
        + "];if(e)e.click();return !!e})()"
    )
    if not clicked:
        raise ValueError("交易日期筛选入口发生变化")


async def collect(page, options):
    if options["trade_range"] == "realtime":
        report(page, stage="等待交易概况与成交趋势")
        raw = await wait_responses(page, CORE_ENDPOINTS)
        mapping = await display_map(page, raw)
        brief = normalize({**{name: {} for name in ENDPOINTS}, **raw}, mapping, options, await source_time(page))
        # Auxiliary widgets must not erase their last successful values when
        # only the homepage summary has been read.
        brief["metrics"] = {
            key: value
            for key, value in brief["metrics"].items()
            if key not in ("annual", "regions", "orders_info", "lead")
        }
        brief.update(summary_complete=True, complete=False)
        from .summary_publication import publish, only

        if only("transactions", options):
            brief["details_on_demand"] = True
            return brief
        await publish("transactions", options, brief)
        report(page, stage="交易概况与趋势已核验，读取附属统计")
        raw.update(await wait_responses(page, ENDPOINTS - CORE_ENDPOINTS))
    else:
        raw = await wait_responses(page, ENDPOINTS)
        names = CORE_ENDPOINTS
        page.reset_trade(names)
        await click_range(page, RANGES[options["trade_range"]])
        raw.update(await wait_responses(page, names))
    if options["trade_range"] in ("week", "month"):
        page.reset_trade(("queryMallGeographyDistributionList",))
        await click_range(page, RANGES[options["trade_range"]], last=True)
        raw.update(await wait_responses(page, ("queryMallGeographyDistributionList",)))
    mapping = await display_map(page, raw)
    return normalize(raw, mapping, options, await source_time(page))


async def source_time(page):
    return await page.evaluate(
        "(()=>{const t=document.body.innerText.split('交易概况')[1]||'';return (t.match(/统计时间[：:]\\s*([^\\n]+)/)||[])[1]||null})()"
    )


def summarize(metrics):
    totals = {}
    for key in ("sales", "orders", "buyers", "refund_amount", "refund_orders"):
        values = [Decimal(str(row["summary"][key])) for row in metrics if row.get("summary", {}).get(key) is not None]
        totals[key] = float(sum(values)) if values else None
    return totals
