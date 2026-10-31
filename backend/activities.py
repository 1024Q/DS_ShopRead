"""Read the merchant's recent activity enrollment records, without business mutations."""

import asyncio
import json
from pathlib import Path

STATUS_DEFINITIONS = json.loads(
    (Path(__file__).resolve().parents[1] / "config/pdd-activity-statuses.json").read_text(encoding="utf8")
)
from .business_reader import checked_result, date_text
from .product_images import thumbnail
from .sync_progress import report
from .read_errors import PageReadError

LIMIT = 2000


def groups(row, definitions=None):
    if definitions is None:
        definitions = STATUS_DEFINITIONS
    names = {1: "待处理", 4: "审核中", 2: "活动中", 3: "已结束"}
    result = []
    for entry in definitions:
        if entry.get("status_type") not in names:
            continue
        if any(
            str(row.get("status_code")) == str(code.get("status"))
            and (code.get("sub_status") is None or str(row.get("sub_status_code")) == str(code["sub_status"]))
            for code in entry.get("status_code_list", [])
        ):
            result.append(names[entry["status_type"]])
    if row.get("wait_handle_price") and "待处理" not in result:
        result.append("待处理")
    return list(dict.fromkeys(result))


def amount(value):
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError("活动金额格式发生变化，未保存本次结果")
    return value / 100


def normalize(item):
    if not isinstance(item, dict) or not item.get("activity_goods_id") or not item.get("goods_id"):
        raise ValueError("活动报名记录缺少报名编号或商品编号")
    status = item.get("final_status_name") or "未知状态 " + str(item.get("status", "未提供"))
    raw_slots = item.get("activity_goods_time_list") or []
    raw_skus = item.get("activity_sku_info") or []
    if not isinstance(raw_slots, list) or not isinstance(raw_skus, list):
        raise ValueError("活动排期或规格格式发生变化")
    slots = [
        {
            "start": date_text(s.get("activity_goods_start_time")),
            "end": date_text(s.get("activity_goods_end_time")),
            "price_min": amount(s.get("min_activity_sku_price")),
            "price_max": amount(s.get("max_activity_sku_price")),
            "quantity": s.get("activity_quantity"),
            "event_id": str(s.get("activity_goods_event_id") or ""),
        }
        for s in raw_slots
    ]
    skus = [
        {
            "sku_id": str(s.get("sku_id") or ""),
            "name": s.get("sku_name") or "",
            "price": amount(s.get("activity_price")),
            "on_sale": s.get("is_on_sale"),
        }
        for s in raw_skus
    ]
    lows = [s["price_min"] for s in slots if s["price_min"] is not None]
    highs = [s["price_max"] for s in slots if s["price_max"] is not None]
    prices = [s["price"] for s in skus if s["price"] is not None]
    reason = "；".join(
        dict.fromkeys(str(item[k]) for k in ("refuse_reason", "cancel_reason", "extension_audit_reason") if item.get(k))
    )
    return {
        "id": str(item["activity_goods_id"]),
        "goods_id": str(item["goods_id"]),
        "name": item.get("goods_name") or "",
        "thumbnail_url": thumbnail(item),
        "activity_id": str(item.get("activity_id") or ""),
        "activity_name": item.get("activity_name") or "",
        "activity_type": item.get("business_type_str") or "类型 " + str(item.get("activity_type", "未提供")),
        "wait_handle_price": bool(item.get("is_wait_handle_invite_cut_price")),
        "status": status,
        "status_code": item.get("status"),
        "sub_status_code": item.get("sub_status"),
        "created_at": date_text(item.get("enroll_time")),
        "price_min": min(lows or prices) if lows or prices else None,
        "price_max": max(highs or prices) if highs or prices else None,
        "problem": reason,
        "schedule": slots,
        "skus": skus,
        "activity_quantity": item.get("activity_quantity"),
        "activity_description": item.get("activity_description") or "",
        "outer_price_min": amount((item.get("outer_activity_price_vo") or {}).get("min_fair_activity_price")),
    }


async def collect(page, options):
    # The page supplies required authentication. Observe its normal requests and
    # advance only the native read-only pager, never replay unsigned list calls.
    import json

    definitions = checked_result(
        json.loads(
            await page.evaluate(
                "fetch('/lakemms/activityGoods/statusType',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:'{}'}).then(r=>r.text())"
            )
        )
    )
    if not isinstance(definitions, list):
        raise ValueError("活动状态分类格式发生变化")
    rows = []
    seen = set()
    complete = False
    total = None
    number = 1
    while len(rows) < LIMIT:
        try:
            async with asyncio.timeout(25):
                while True:
                    message = await page.activities.get()
                    if message["page"] == number:
                        break
        except TimeoutError:
            raise PageReadError("未收到活动报名页面数据，请稍后刷新或打开后台检查")
        result = checked_result(message["data"])
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("result"), list)
            or type(result.get("total")) is not int
            or result["total"] < 0
        ):
            raise ValueError("活动报名列表格式发生变化，保留上次数据")
        total = result["total"]
        batch = result["result"]
        before = len(rows)
        for raw in batch:
            row = normalize(raw)
            row["activity_groups"] = groups(row, definitions)
            if row["id"] not in seen:
                seen.add(row["id"])
                rows.append(row)
        report(page, len(rows), total, "读取活动报名记录")
        if len(rows) >= total:
            complete = True
            break
        if not batch or len(rows) == before:
            break
        await asyncio.sleep(0.5)
        advanced = await page.evaluate(
            "(()=>{const e=document.querySelector('[data-testid=beast-core-pagination-next]');if(!e||e.getAttribute('aria-disabled')==='true'||e.className.includes('disabled'))return false;e.click();return true;})()"
        )
        if not advanced:
            break
        number += 1
    return {
        "rows": rows[:LIMIT],
        "source_total": total,
        "source_loaded": min(len(rows), LIMIT),
        "source_unit": "报名记录",
        "complete": complete and len(rows) <= LIMIT,
        "limit": LIMIT,
        "source_scope": "平台近期报名记录，不含已归档记录",
    }
