"""Read the cashier's published daily/monthly goods-payment summaries."""

import json
import asyncio
import re
from datetime import date, datetime, timedelta
from .business_reader import BEIJING, checked_result

KINDS = {"reconciliation_day", "reconciliation_month"}
APIS = {
    "reconciliation_day": "/templar/api/bill/queryDailyBills?__app_code=113",
    "reconciliation_month": "/templar/api/bill/queryMallBalanceMonthlySummary?__app_code=113",
}
DETAIL_API = "/templar/api/bill/queryDailyBillDetailStatistic?__app_code=113"


def normalize_details(result, row):
    if not isinstance(result, dict):
        raise ValueError("账单展开明细格式发生变化")
    details = {}
    for side in ("income", "outcome"):
        items = result.get(side)
        if not isinstance(items, list):
            raise ValueError("账单展开明细缺少收入或支出分类")
        values = []
        seen = set()
        for item in items:
            if (
                not isinstance(item, dict)
                or type(item.get("amount")) is not int
                or type(item.get("number")) is not int
                or item["number"] < 0
                or type(item.get("financeId")) is not int
                or not isinstance(item.get("description"), str)
                or not item["description"].strip()
            ):
                raise ValueError("账单分类金额、笔数或科目格式发生变化")
            if item["financeId"] in seen:
                raise ValueError("账单展开明细存在重复科目")
            seen.add(item["financeId"])
            if (side == "income" and item["amount"] < 0) or (side == "outcome" and item["amount"] > 0):
                raise ValueError("账单分类收支正负号异常")
            values.append(
                {
                    "finance_id": item["financeId"],
                    "label": item["description"],
                    "amount_cents": item["amount"],
                    "amount": item["amount"] / 100,
                    "count": item["number"],
                }
            )
        if (
            sum(v["amount_cents"] for v in values) != row[side + "_cents"]
            or sum(v["count"] for v in values) != row[side + "_count"]
        ):
            raise ValueError("分类明细金额或笔数与账单汇总不一致，保留上次数据")
        details[side] = values
    return details


async def read_breakdown(page, row, kind, latest, index=0):
    if index and index % 4 == 0:
        await page.prepare_reconciliation(
            "reconciliation_month" if kind == "reconciliation_day" else "reconciliation_day"
        )
        await page.prepare_reconciliation(kind)
    start = date.fromisoformat(row["created_at"])
    end = start + timedelta(days=1) if kind == "reconciliation_day" else next_month(start)
    if kind == "reconciliation_month":
        if not latest:
            raise ValueError("平台未提供账单截至日期，未读取展开明细")
        end = min(end, date.fromisoformat(latest) + timedelta(days=1))
    if end <= start:
        raise ValueError("所选账期尚未发布")
    body = {"beginDate": start.isoformat(), "endDate": end.isoformat()}
    headers = {**page.finance_headers, "Content-Type": "application/json"}
    raw = await page.evaluate(
        '(async()=>{if(location.hostname!=="cashier.pinduoduo.com")throw Error("资金页面尚未就绪");const r=await fetch('
        + json.dumps(DETAIL_API)
        + ',{method:"POST",credentials:"include",headers:'
        + json.dumps(headers)
        + ",body:JSON.stringify("
        + json.dumps(body)
        + '),signal:AbortSignal.timeout(15000)});if(!r.ok)throw Error("展开明细读取失败");return await r.text();})()'
    )
    return checked_result(json.loads(raw)), (end - timedelta(days=1)).isoformat()


def next_month(day):
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)


def options_for(kind, period="", today=None):
    if kind not in KINDS:
        raise ValueError("账单类型无效")
    today = today or datetime.now(BEIJING).date()
    if kind == "reconciliation_day":
        period = period or today.strftime("%Y-%m")
        if not re.fullmatch(r"\d{4}-\d{2}", period):
            raise ValueError("请选择有效的入账月份")
        try:
            first = date.fromisoformat(period + "-01")
        except ValueError:
            raise ValueError("请选择有效的入账月份") from None
        if first > today or first.year < 2000:
            raise ValueError("入账月份必须在 2000 年至当前月份之间")
        end = next_month(first)
    else:
        period = period or str(today.year)
        if not re.fullmatch(r"\d{4}", period) or not 2000 <= int(period) <= today.year:
            raise ValueError("请选择 2000 年至今年之间的入账年份")
        first = date(int(period), 1, 1)
        end = next_month(today) if first.year == today.year else date(first.year + 1, 1, 1)
    return {
        "period": period,
        "date_from": first.isoformat(),
        "date_to": end.isoformat(),
        "status": "",
        "stars": [],
        "reply": "",
    }


def normalize(item, kind):
    if not isinstance(item, dict):
        raise ValueError("账单汇总格式发生变化")
    period = item.get("time")
    pattern = r"\d{4}-\d{2}-\d{2}" if kind == "reconciliation_day" else r"\d{4}-\d{2}"
    if not isinstance(period, str) or not re.fullmatch(pattern, period):
        raise ValueError("账单日期格式发生变化")
    date.fromisoformat(period if kind == "reconciliation_day" else period + "-01")
    row = {
        "id": period,
        "period": period,
        "name": period,
        "created_at": period if kind == "reconciliation_day" else period + "-01",
    }
    for target, source in {
        "income": "incomeAmount",
        "outcome": "outcomeAmount",
        "opening_balance": "beforeAmount",
        "closing_balance": "afterAmount",
    }.items():
        value = item.get(source)
        if type(value) is not int:
            raise ValueError("账单金额字段缺失或格式发生变化")
        row[target + "_cents"] = value
        row[target] = value / 100
    for target, source in {"income_count": "incomeNumber", "outcome_count": "outcomeNumber"}.items():
        value = item.get(source)
        if type(value) is not int or value < 0:
            raise ValueError("账单笔数字段缺失或格式发生变化")
        row[target] = value
    row["net_cents"] = row["income_cents"] + row["outcome_cents"]
    row["net"] = row["net_cents"] / 100
    return row


def summarize(rows):
    # Add integer cents, retaining the platform's signed expenditure values.
    if not rows:
        return {key: None for key in ("income", "outcome", "net", "income_count", "outcome_count")}
    return {
        **{key: sum(row[key + "_cents"] for row in rows) / 100 for key in ("income", "outcome", "net")},
        **{key: sum(row[key] for row in rows) for key in ("income_count", "outcome_count")},
    }


async def collect(page, kind, options, include_details=True):
    headers = {**await page.prepare_reconciliation(kind), "Content-Type": "application/json"}
    if kind == "reconciliation_day":
        body = {"beginDate": options["date_from"], "endDate": options["date_to"]}
    else:
        body = {"beginMonth": options["date_from"][:7], "endMonth": options["date_to"][:7]}
    expression = (
        '(async()=>{if(location.hostname!=="cashier.pinduoduo.com")throw Error("资金页面尚未就绪");const r=await fetch('
        + json.dumps(APIS[kind])
        + ',{method:"POST",credentials:"include",headers:'
        + json.dumps(headers)
        + ",body:JSON.stringify("
        + json.dumps(body)
        + '),signal:AbortSignal.timeout(15000)});if(!r.ok)throw Error("读取失败");return await r.text();})()'
    )
    result = checked_result(json.loads(await page.evaluate(expression)))
    if kind == "reconciliation_day":
        items = result
        latest = None
    else:
        if not isinstance(result, dict) or "summaryList" not in result:
            raise ValueError("未读取到平台月汇总数据")
        items = result["summaryList"]
        latest = result.get("latestSummaryDay")
    if not isinstance(items, list):
        raise ValueError("账单汇总格式发生变化，未覆盖上次数据")
    rows = [normalize(item, kind) for item in items]
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("平台返回重复账期，请稍后重新同步")
    for row in rows:
        if not options["date_from"] <= row["created_at"] < options["date_to"]:
            raise ValueError("平台返回账期超出查询范围")
        row["summary_through"] = latest if latest and latest[:7] == row["period"] else ""
    if kind == "reconciliation_day":
        latest = max((row["period"] for row in rows), default=None)
    if include_details:
        for index, row in enumerate(rows):
            from .sync_progress import report

            report(page, index, len(rows), "读取收支分类明细")
            raw, through = await read_breakdown(page, row, kind, latest, index)
            row["breakdown"] = normalize_details(raw, row)
            row["details_through"] = through
            await asyncio.sleep(0.1)
    return {
        "rows": rows,
        "source_total": len(rows),
        "complete": True,
        "latest_summary_day": latest,
        "source_unit": "账期",
    }
