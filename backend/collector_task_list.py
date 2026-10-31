from .read_budget import remaining as read_remaining

"""Bounded local GUI task details, using the same sets as the summary counters."""
from .shop_identity import shop_label
from .collection import DEFAULTS
from .task_catalog import title as task_title
import time


def deferred_categories(service):
    from .master_tasks import enabled, listing as masters

    if enabled(getattr(service, "queue_config", {})):
        return masters(service, "deferred", 1)["categories"]
    result = {}
    for key, meta in list(getattr(service, "deferred", {}).items()):
        if key in service.progress:
            continue
        category = meta.get("stage", "到期待执行")
        if category == "等待采集名额":
            category = "到期待执行"
        result[category] = result.get(category, 0) + 1
    return result


def listing(service, group, page, cached, errors, waiting):
    if group not in ("running", "queued", "errors", "waiting_login", "deferred", "cached"):
        raise ValueError("任务类别无效")
    from .master_tasks import enabled, listing as masters

    if enabled(getattr(service, "queue_config", {})) and group in ("running", "queued", "deferred"):
        return masters(service, group, page)
    shops = {s["id"]: s for s in service.store.list()}
    rows = []
    categories = {}

    def add(sid, kind, detail="", scope=None, meta=None):
        scope = scope or {}
        lane = {
            "recent": "近期订单",
            "open": "未完结回查",
            "history": "历史补采",
            "daily": "历史核对",
            "recheck": "订单状态核对",
        }.get(scope.get("_order_lane"), "")
        from .demand_refresh import timing
        from datetime import datetime
        from .business_reader import BEIJING

        times = (
            timing(
                kind,
                scope,
                meta,
                getattr(service, "queue_config", DEFAULTS),
                shops.get(sid, {}).get("platform", "pdd"),
                time.time(),
            )
            if meta and meta.get("stage") != "等待下次执行"
            else {}
        )
        if times:
            detail += f" · 累计排队 {times['wait_seconds']} 秒"
            if times["overdue_seconds"]:
                detail += f" · 超期 {times['overdue_seconds']} 秒"
            if times["last_merged_at"]:
                detail += " · 合并 " + datetime.fromtimestamp(times["last_merged_at"], BEIJING).strftime(
                    "%m-%d %H:%M:%S"
                )
        from .frequency_policy import group as frequency_group

        config = getattr(service, "queue_config", DEFAULTS)
        g = (
            frequency_group(kind, scope, config, shops.get(sid, {}).get("platform", "pdd"))
            if config.get("frequency_version")
            else None
        )
        meta = meta or {}
        from .task_timing import text as timing_text, fields as timing_fields

        if timing_text(meta):
            detail += " · " + timing_text(meta)
        started = meta.get("started_at")
        elapsed = max(0, int(time.time() - started)) if started else None
        stamp = lambda v: datetime.fromtimestamp(v, BEIJING).strftime("%m-%d %H:%M:%S") if v else "—"
        rows.append(
            {
                **times,
                **timing_fields(meta),
                "frequency": g["name"] if g else "—",
                "started": stamp(started),
                "elapsed": str(elapsed) + " 秒" if started else "—",
                "remaining": "入库中 · 独立等待"
                if meta.get("read_finished")
                else "数据库等待 · 读取计时暂停"
                if meta.get("timeout_paused")
                else str(read_remaining(meta, time.time())) + " 秒"
                if started
                else "未开始计时",
                "next_run": stamp(meta.get("next_run_at")),
                "generated": datetime.fromtimestamp(times["generated_at"], BEIJING).strftime("%m-%d %H:%M:%S")
                if times
                else "—",
                "shop": shop_label(shops.get(sid, {"id": sid})),
                "module": task_title(kind, scope),
                "detail": detail,
                "scope": " · ".join(
                    filter(
                        None,
                        [
                            *[(str(scope.get(k) or "")) for k in ("date_from", "date_to", "status")],
                            "重点状态" if scope.get("_attention") else lane,
                        ],
                    )
                ),
            }
        )

    if group in ("running", "queued"):
        for (sid, key), meta in list(service.progress.items()):
            from .collection_queue import waiting_reason

            if meta.get("phase") == group:
                add(
                    sid,
                    meta.get("kind") or key.split(":")[0],
                    waiting_reason((sid, key), meta, service.progress, service.queue_config)
                    or meta.get("stage")
                    or meta.get("wait_reason")
                    or ("读取中" if group == "running" else "等待可用名额"),
                    meta.get("scope"),
                    meta,
                )
    elif group == "deferred":
        for (sid, key), meta in list(getattr(service, "deferred", {}).items()):
            if (sid, key) in service.progress:
                continue
            category = meta.get("stage", "到期待执行")
            if category == "等待采集名额":
                category = "到期待执行"
            categories[category] = categories.get(category, 0) + 1
            detail = category
            if meta.get("retry_at"):
                detail += f" · {max(0, int(meta['retry_at'] - time.time()))} 秒后重试"
            if meta.get("next_run_at"):
                detail += f" · {max(0, int(meta['next_run_at'] - time.time()))} 秒后到期"
            if meta.get("reason"):
                detail += " · " + meta["reason"]
            add(sid, meta["kind"], detail, meta.get("scope"), meta)
    else:
        for sid, kind in cached if group == "cached" else errors if group == "errors" else waiting:
            add(
                sid,
                kind,
                errors.get((sid, kind), "")
                if group == "errors"
                else "已写入本机数据库；中心接收情况见回传状态"
                if group == "cached"
                else "请完成登录验证",
            )
    rows.sort(key=lambda r: (r["shop"], r["module"], r["scope"]))
    pages = max(1, (len(rows) + 29) // 30)
    page = min(page, pages)
    return {
        "rows": rows[(page - 1) * 30 : page * 30],
        "total": len(rows),
        "page": page,
        "page_count": pages,
        "categories": categories,
    }
