from .shop_identity import shop_label

"""Shop identity and the user's four-category monthly reporting calculation."""
import asyncio
import json
import hashlib
import re
from datetime import date, datetime, timedelta
from .business_reader import BEIJING, ReadPage, checked_result
from . import reconciliation
from .sync_progress import report

FIELDS = {
    "trade_income": ("income", 1, "交易收入"),
    "coupon_subsidy": ("income", 2, "优惠券补贴"),
    "refund": ("outcome", 6, "退款"),
    "coupon_refund": ("outcome", 7, "退款退优惠券"),
}
DETAIL_API = "/templar/api/bill/queryDailyBillDetailStatistic?__app_code=113"


def options_for(period="", today=None):
    today = today or datetime.now(BEIJING).date()
    period = period or today.strftime("%Y-%m")
    quarter = re.fullmatch(r"(\d{4})-Q([1-4])", period)
    if quarter:
        year, q = map(int, quarter.groups())
        if not 2000 <= year <= today.year:
            raise ValueError("入账年份必须在 2000 年至今年之间")
        first = date(year, (q - 1) * 3 + 1, 1)
        if first > today:
            raise ValueError("不能查询尚未开始的季度")
        end = date(year + 1, 1, 1) if q == 4 else date(year, q * 3 + 1, 1)
        end = min(end, reconciliation.next_month(today))
        return {
            "period": period,
            "date_from": first.isoformat(),
            "date_to": end.isoformat(),
            "status": "",
            "stars": [],
            "reply": "",
        }
    kind = "reconciliation_day" if re.fullmatch(r"\d{4}-\d{2}", period) else "reconciliation_month"
    return reconciliation.options_for(kind, period, today)


def subject_key(entity, shop_id=None):
    entity = entity or {}
    code = (entity.get("credit_code") or "").strip().upper()
    identity = "code:" + code if code and "*" not in code else "name:" + (entity.get("company_name") or "").strip()
    if identity == "name:":
        identity = "shop:" + str(shop_id)
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def subject_options(sources):
    subjects = {}
    for source in sources:
        entity = source.get("entity")
        if not entity:
            continue
        key = subject_key(entity, source["shop_id"])
        subject = subjects.setdefault(
            key, {"id": key, "name": entity["company_name"], "entity": entity, "shop_names": [], "shop_labels": []}
        )
        subject["company_changed"] = subject.get("company_changed", False) or source.get("db_comparison", {}).get(
            "company_changed", False
        )
        if shop_label(source) not in subject["shop_labels"]:
            subject["shop_names"].append(source["shop_name"])
            subject["shop_labels"].append(shop_label(source))
    return sorted(subjects.values(), key=lambda item: (item["name"], item["id"]))


def group_rows(rows, subject=""):
    grouped = {}
    official = []
    for row in rows:
        key = subject_key(row.get("entity"), row["shop_id"])
        if subject and key != subject:
            continue
        if row.get("official_tax"):
            official.append(
                {
                    **row,
                    "subject_id": key,
                    "shop_names": [row["shop_name"]],
                    "shop_breakdown": [row],
                    "db_changed": row.get("db_comparison", {}).get("attention", False),
                }
            )
            continue
        group = grouped.setdefault(
            (key, row["period"]),
            {
                **row,
                "id": key + ":" + row["period"],
                "shop_id": key,
                "subject_id": key,
                "shop_names": [],
                "shop_breakdown": [],
                "db_changed": False,
                "db_missing": False,
                **{field + "_cents": 0 for field in (*FIELDS, "tax_amount")},
            },
        )
        group["shop_names"].append(row["shop_name"])
        group["shop_breakdown"].append(row)
        compare = row.get("db_comparison", {})
        group["db_changed"] = group["db_changed"] or compare.get("attention", compare.get("changed", False))
        group["current_month"] = compare.get("current_month", False)
        group["db_missing"] = group["db_missing"] or row.get("db_comparison", {}).get("missing", False)
        group["summary_through"] = min(group["summary_through"], row["summary_through"])
        group["partial"] = group["partial"] or row["partial"]
        for field in (*FIELDS, "tax_amount"):
            group[field + "_cents"] += row[field + "_cents"]
    for group in grouped.values():
        changed = [
            r
            for r in group["shop_breakdown"]
            if r.get("db_comparison", {}).get("attention", r.get("db_comparison", {}).get("changed"))
        ]
        group["db_reviewed"] = bool(changed) and all(
            r.get("db_comparison", {}).get("review", {}).get("confirmed") for r in changed
        )
        group["operators"] = sorted({name for row in group["shop_breakdown"] for name in row.get("operators", [])})
        group["shop_name"] = "、".join(group["shop_names"])
        group["shop_label"] = "、".join(shop_label(r) for r in group["shop_breakdown"])
        for field in (*FIELDS, "tax_amount"):
            group[field] = group[field + "_cents"] / 100
    return list(grouped.values()) + official


def normalize_entity(result, subject_type=""):
    if not isinstance(result, dict):
        raise ValueError("主体信息格式发生变化")
    details = result.get("queryDetailResult") or {}
    enterprise = details.get("enterprise") or {}
    if not enterprise.get("companyName"):
        raise ValueError("未读取到企业主体名称，请检查主体信息权限")
    # Persist only reporting fields, never credentials, ID images or ID numbers.
    fields = {
        "company_name": "companyName",
        "credit_code": "socialCreditUnicode",
        "taxpayer_number": "taxpayerIdentityNumber",
        "legal_representative": "legalRepresentativeName",
        "company_type": "companyType",
        "registration_status": "companyRegisterState",
        "registered_address": "companyRegisterAddress",
        "business_address": "companyAddress",
        "business_scope": "businessScope",
        "license_start": "businessLicenceStartTime",
        "license_end": "businessLicenceEndTime",
    }
    entity = {target: enterprise.get(source) or "" for target, source in fields.items()}
    if entity["license_end"] == "9999-09-09":
        entity["license_end"] = "长期"
    entity.update(
        subject_type=subject_type,
        platform_shop_name=details.get("mallName") or "",
        mall_id=str(details.get("mallId") or ""),
    )
    return entity


def normalize_breakdown(result, monthly, entity, through):
    if not isinstance(result, dict) or any(not isinstance(result.get(side), list) for side in ("income", "outcome")):
        raise ValueError("月汇总展开明细不完整，未计算报税金额")
    found = {}
    for side in ("income", "outcome"):
        seen = set()
        total = 0
        for item in result[side]:
            if (
                not isinstance(item, dict)
                or type(item.get("amount")) is not int
                or type(item.get("financeId")) is not int
            ):
                raise ValueError("月汇总科目或金额格式发生变化")
            code, amount = item["financeId"], item["amount"]
            if code in seen:
                raise ValueError("月汇总存在重复科目")
            seen.add(code)
            total += amount
            if (side == "income" and amount < 0) or (side == "outcome" and amount > 0):
                raise ValueError("月汇总收支正负号异常")
            for key, (expected_side, expected_code, label) in FIELDS.items():
                if code == expected_code or item.get("description") == label:
                    if side != expected_side or code != expected_code or item.get("description") != label:
                        raise ValueError("月汇总科目对应关系发生变化")
                    found[key] = amount if side == "income" else -amount
        if total != monthly[side + "_cents"]:
            raise ValueError("展开科目与月汇总总额不一致，请稍后重试")
    # Platform omits zero-activity categories; zero is safe only after totals agree.
    row = {key + "_cents": found.get(key, 0) for key in FIELDS}
    row["tax_amount_cents"] = (
        row["trade_income_cents"] + row["coupon_subsidy_cents"] - row["refund_cents"] - row["coupon_refund_cents"]
    )
    for key in (*FIELDS, "tax_amount"):
        row[key] = row[key + "_cents"] / 100
    row.update(
        id=monthly["period"],
        period=monthly["period"],
        created_at=monthly["created_at"],
        name=entity["company_name"],
        entity=entity,
        summary_through=through,
        partial=through
        < (reconciliation.next_month(date.fromisoformat(monthly["created_at"])) - timedelta(days=1)).isoformat(),
    )
    return row


def summarize(rows):
    pdd = [r for r in rows if not r.get("official_tax")]
    official = [r for r in rows if r.get("official_tax")]
    return {
        **{key: sum(row[key + "_cents"] for row in pdd) / 100 if pdd else None for key in (*FIELDS, "tax_amount")},
        "official_net": sum(r["tax_amount_cents"] for r in official) / 100 if official else None,
        "official_count": len(official),
    }


async def collect(session, options):
    async with ReadPage(session) as page:
        await page.open("entity")
        for _ in range(35):
            clicked = await page.evaluate(
                "(()=>{const e=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&e.textContent.trim()==='主体信息'&&e.offsetWidth>0);if(e)e.click();return !!e;})()"
            )
            if clicked:
                break
            await asyncio.sleep(0.3)
        if not clicked:
            raise ValueError("主体信息页面尚未加载完成，请稍后重试")
        subject_type = ""
        for _ in range(20):
            content = await page.evaluate("document.body.innerText")
            match = re.search(r"主体类型\s*\n([^\n]+)", content)
            if match:
                subject_type = match[1].strip()
                break
            await asyncio.sleep(0.2)
        raw = await page.evaluate(
            '(async()=>{const r=await fetch("/earth/api/mallInfo/queryFinalCredentialNew",{credentials:"include",signal:AbortSignal.timeout(15000)});if(!r.ok)throw Error("主体信息读取失败");return await r.text();})()'
        )
        entity = normalize_entity(checked_result(json.loads(raw)), subject_type)
        await page.open("reconciliation_month")
        data = await reconciliation.collect(page, "reconciliation_month", options, include_details=False)
        rows = []
        for index, monthly in enumerate(data["rows"]):
            report(page, index, len(data["rows"]), "读取报税月度明细")
            # Annual reads outlive a cashier request's short-lived headers.
            # Let the original UI issue fresh authenticated read requests.
            if index and index % 4 == 0:
                await page.prepare_reconciliation("reconciliation_day")
                await page.prepare_reconciliation("reconciliation_month")
            latest = data.get("latest_summary_day")
            if not latest:
                raise ValueError("平台未提供账单截至日期，未计算报税金额")
            start = date.fromisoformat(monthly["created_at"])
            end = min(reconciliation.next_month(start), date.fromisoformat(latest) + timedelta(days=1))
            if end <= start:
                raise ValueError("所选月份账单尚未发布")
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
            rows.append(
                normalize_breakdown(
                    checked_result(json.loads(raw)), monthly, entity, (end - timedelta(days=1)).isoformat()
                )
            )
            await asyncio.sleep(0.1)
        return {
            **data,
            "rows": rows,
            "entity": entity,
            "source_total": len(rows),
            "source_unit": "月账单",
            "latest_summary_day": max((row["summary_through"] for row in rows), default=None),
        }
