"""Read-only operation history, using the official operation-log query contract."""

import hashlib
import json
from datetime import datetime
from .business_reader import date_text
from .sync_progress import report

URL = "https://yingxiao.pinduoduo.com/tools/operate/log"
ENDPOINT = "/mms-gateway/venus/api/log/queryOperationLogWithCount"
MODULES = [
    "全部",
    "计划",
    "单元",
    "关键词",
    "人群",
    "资源位",
    "创意",
    "品牌类目词",
    "人群",
    "财务",
    "产品",
    "店铺",
    "计划组",
    "活动",
    "商品",
    "推广",
    "账户",
]
ACTIONS = {1: "添加", 2: "更新", 3: "删除"}


def normalize(raw, plate=15):
    if not isinstance(raw, dict) or not raw.get("operateTime") or "content" not in raw:
        raise ValueError("推广操作记录字段发生变化，保留上次数据")
    module = raw.get("moduleType")
    action = ACTIONS.get(raw.get("operationType"), "其他操作")
    module_name = MODULES[module] if type(module) is int and 0 <= module < len(MODULES) else "其他模块"
    identity = (
        raw.get("id")
        or raw.get("logId")
        or hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
    )
    value = raw["operateTime"]
    created = (
        datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(value, str) and "-" in value
        else date_text(value)
    )
    return {
        "id": str(plate) + ":" + str(identity),
        "created_at": created,
        "operator_name": str(raw.get("operatorName") or ""),
        "name": str(raw.get("operationObject") or ""),
        "status": action,
        "operation_module": module_name,
        "content": str(raw.get("content") or ""),
        "operation_type": str(raw.get("operationType") or ""),
        "plate_type": plate,
    }


async def collect(session, options):
    from .promotions import PromotionPage, LOGS
    from .request_reuse import owner, check_cooldown, paced

    root = owner(session)
    # Native page actions also hit the log endpoint. Do not open a tab while it is cooling.
    check_cooldown(root, LOGS)

    async with PromotionPage(session) as page:
        await page.call("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Shanghai"})
        rows = []
        seen = set()
        total = 0
        complete = True
        limit = 2000
        # Navigate native read-only tabs; the site signs its own requests.
        for tab, plate in ((1007, 15), (6, 6), (1, 1), (14, 14), (4, 4)):
            await paced(root, LOGS, lambda: page.call("Page.navigate", {"url": URL + "?activeKey=" + str(tab)}))
            plate_loaded = 0
            plate_done = False
            for number in range(1, 201):
                data, scope = await page.wait(LOGS, options, number, plate=plate)
                if scope.get("plateType") != plate:
                    raise ValueError("操作记录产品线不匹配")
                if (
                    not isinstance(data, dict)
                    or not isinstance(data.get("logList"), list)
                    or type(data.get("total")) is not int
                ):
                    raise ValueError("推广操作记录分页格式发生变化，保留上次数据")
                if number == 1:
                    total += data["total"]
                before = len(rows)
                for raw in data["logList"]:
                    row = normalize(raw, plate)
                    if row["id"] not in seen:
                        seen.add(row["id"])
                        rows.append(row)
                        plate_loaded += 1
                report(page, len(rows), total, "读取推广操作记录")
                if not data.get("totalOverThreshold") and plate_loaded >= data["total"]:
                    plate_done = True
                    break
                if len(rows) >= limit or len(rows) == before:
                    break
                async def advance():
                    # Let the platform settle after a response before the next native pagination click.
                    import asyncio
                    await asyncio.sleep(1)
                    check_cooldown(root, LOGS)
                    return await page.evaluate(
                    "(()=>{const e=[...document.querySelectorAll('.anq-pagination-next')].find(e=>e.offsetWidth>0);if(!e||e.classList.contains('anq-pagination-disabled'))return false;(e.querySelector('button')||e).click();return true;})()"
                    )
                advanced = await paced(root, LOGS, advance)
                if not advanced:
                    break
            complete &= plate_done
            if len(rows) >= limit:
                complete = False
                break
        return {
            "rows": rows[:limit],
            "source_total": total,
            "complete": complete,
            "limit": limit,
            "source_unit": "操作记录",
            "source_scope": "近7天，商品推广、直播推广、明星店铺、整合营销及账户操作",
        }
