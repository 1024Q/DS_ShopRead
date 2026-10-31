"""Verified homepage counters from the exact filtered list read; never collect."""

from datetime import datetime
from .business_reader import BEIJING

STATUSES = {"orders": {"待发货", "即将逾期发货"}, "aftersales": {"待商家处理", "待处理即将逾期"}, "tickets": {"待处理"}}


def source_fields(payload):
    scope = payload.get("_sync", {}).get("scope")
    if not scope and payload.get("_order_rows_v2"):
        days = sorted(payload.get("_order_days", {}))
        if days:
            scope = {"status": "", "date_from": days[0], "date_to": days[-1]}
    return {"snapshot_scope": scope or {}, "list_coverage": payload.get("_list_coverage")}


def verify(kind, options, source):
    if not source.get("updated_at"):
        return False, "对应列表尚未读取"
    if not source.get("complete"):
        return False, "对应列表尚未读取完整"
    scope = source.get("snapshot_scope") or {}
    if not scope:
        return False, "历史快照缺少可核验的采集范围"
    if scope.get("_attention"):
        return False, "仅有重点状态快照，不能代表全部待办"
    status = scope.get("status", "")
    wanted = options.get("status", "")
    if status and status != wanted:
        return False, "已存快照的订单状态与目标列表不同"
    # PDD merchant-pending and expiring shortcuts deliberately have no age limit.
    if (
        kind == "aftersales"
        and source.get("platform", "pdd") == "pdd"
        and not options.get("explicit_dates")
        and wanted in STATUSES[kind]
    ):
        if status != wanted or scope.get("explicit_dates"):
            return False, "尚无不限日期的对应售后状态完整快照"
    else:
        if not scope.get("date_from") or not scope.get("date_to"):
            return False, "历史快照缺少可核验的日期范围"
        if scope["date_from"] > options.get("date_from", "") or scope["date_to"] < options.get("date_to", "9999"):
            return False, "已存快照未覆盖目标列表日期范围"
    return True, ""


def project(data, kind, options):
    counts = data.get("shop_counts", {})
    rows = []
    for source in data.get("sources", []):
        verified, reason = verify(kind, options, source)
        sid = source["shop_id"]
        rows.append(
            {
                "shop_id": sid,
                "count": counts.get(sid, counts.get(str(sid), 0)),
                "verified": verified,
                "reason": reason,
                "updated_at": source.get("updated_at"),
                "snapshot_scope": source.get("snapshot_scope", {}),
                **freshness(source),
            }
        )
    return {"rows": rows, "snapshot_at": datetime.now(BEIJING).isoformat()}


def freshness(source):
    paused = (
        source.get("paused")
        if "paused" in source
        else source.get("freshness") == "paused"
        if source.get("frequency_known")
        else None
    )
    state = (
        "missing"
        if not source.get("updated_at")
        else "offline"
        if source.get("collector_online") is False
        else "paused"
        if paused
        else "waiting_login"
        if source.get("state") and source["state"] not in ("logged_in", "sleeping")
        else "overdue"
        if source.get("overdue") or source.get("stale")
        else source.get("freshness", "unknown")
    )
    reasons = {
        "missing": "尚未读取对应列表",
        "offline": "节点离线，显示上次数据",
        "paused": "对应定时任务已暂停，不会自动更新",
        "waiting_login": "等待店铺登录，显示上次数据",
        "overdue": "超过设定频率，显示上次数据",
        "current": "在设定频率内",
        "unknown": "尚无可核验的新鲜度信息",
    }
    return {
        "freshness_status": state,
        "freshness_reason": reasons.get(state, reasons["unknown"]),
        "collector_online": source.get("collector_online"),
        "paused": paused,
        "last_received_at": source.get("center_received_at"),
    }
