from .filter_scope import matches

"""Lightweight database/runtime projection for today's homepage refresh."""
from datetime import datetime
from .business_reader import BEIJING
from . import collection
from .home_dependencies import scopes as home_scopes
from .shop_identity import identity
from .home_progress_status import waking, overdue_reason


def read(service, platform="", shop_id=None, now=None):
    current = now or datetime.now(BEIJING)
    day = current.date().isoformat()
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    config = collection.settings(service.store)
    rows = []
    live_progress = {key: dict(meta) for key, meta in list(service.progress.items())}
    online = getattr(service, "public_summary", {}).get("collector_online", True)
    shops = [
        s for s in service.store.list() if matches(platform, s.get("platform", "pdd")) and matches(shop_id, s["id"])
    ]
    scoped = {}
    for shop in shops:
        scoped[shop["id"]] = list(home_scopes(shop, at=current))
    from .home_progress_data import read as read_clocks

    tasks, caches, focused = read_clocks(
        service.store, [(s["id"], service.key(k, o, s)) for s in shops for _, k, o, _ in scoped[s["id"]]]
    )
    for shop in shops:
        item = service.session_getter(shop["id"])
        modules = []
        is_waking = waking(item)
        eligible = item.state in ("logged_in", "sleeping") or is_waking
        scopes = scoped[shop["id"]]
        for identifier, kind, options, title in scopes:
            key = service.key(kind, options, shop)
            job = (shop["id"], key)
            progress = live_progress.get(job, {})
            task = tasks.get(job)
            cache = focused.get((shop["id"], kind)) if options.get("_attention") else caches.get(job)
            brief = caches.get((shop["id"], "summary:" + key))
            details_pending = bool(
                brief and brief["summary_complete"] and (not cache or brief["updated"] > cache["updated"])
            )
            details_on_demand = bool(details_pending and brief.get("details_on_demand"))
            if details_pending:
                cache = {**brief, "complete": True}
            updated = cache["updated"] if cache else 0
            complete = bool(cache and cache["complete"] and cache["started"] >= midnight)
            if identifier == "orders@refund":
                complete = bool(
                    complete
                    and cache.get("refund_complete")
                    and cache.get("refund_date") == day
                    and cache.get("refund_basis") == "order_created_success"
                )
            error = task["last_error"] if task else ""
            last_success = (task["last_success"] if task else 0) or (updated if cache and cache["complete"] else 0)
            if details_pending:
                last_success = max(last_success, updated)
            paused = (
                not config["enabled"] or collection.mode(kind, options, config, shop.get("platform", "pdd")) == "off"
            )
            overdue = bool(
                not paused
                and eligible
                and online
                and collection.due(
                    kind,
                    options,
                    last_success,
                    config,
                    current.timestamp(),
                    shop.get("platform", "pdd"),
                    adaptive_seconds=task["adaptive_seconds"] if task else 0,
                )
            )
            retry_at = task["retry_at"] if task else 0
            retry_wait = max(0, int(retry_at - current.timestamp() + 0.999)) if error else 0
            blocked = bool(task and task["blocked"])
            waiting = getattr(service, "deferred", {}).get(job)
            # Waiting for the configured next run is healthy, not admission backlog.
            deferred = waiting is not None and waiting.get("stage") != "等待下次执行"
            pending = (
                "error"
                if blocked
                else "retry_wait"
                if error and retry_wait
                else "deferred"
                if deferred
                else "retry_wait"
                if error and retry_at
                else "error"
                if error
                else "updated"
                if complete and not overdue
                else "waiting"
            )
            stage = progress.get("stage", "") or (
                getattr(service, "deferred", {}).get(job, {}).get("stage", "等待可用采集名额")
                if deferred
                else "等待自动重试"
                if pending == "retry_wait"
                else ""
            )
            if progress.get("phase") == "queued":
                from .collection_queue import waiting_reason, scheduling_config

                stage = waiting_reason(job, progress, live_progress, scheduling_config(service))
            if overdue and not stage and not error:
                stage = "超过设定频率，自动排队补采"
            status = (
                "paused"
                if paused
                else "offline"
                if not online
                else "stopped"
                if item.state == "stopped"
                else "waking"
                if is_waking
                else "login"
                if not eligible
                else progress.get("phase") or ("sleeping" if item.state == "sleeping" else pending)
            )
            if status == "waking":
                stage = "正在唤醒店铺，保留本轮采集名额"
            modules.append(
                {
                    "task_id": identifier,
                    "kind": kind,
                    "title": title,
                    "attention": bool(options.get("_attention")),
                    "status": status,
                    "today_updated": complete,
                    "details_pending": details_pending,
                    "details_on_demand": details_on_demand,
                    "overdue": overdue,
                    "overdue_reason": overdue_reason(status, progress, blocked) if overdue else "",
                    "data_age_seconds": max(0, int(current.timestamp() - last_success)) if last_success else None,
                    "last_collected": datetime.fromtimestamp(last_success, BEIJING).isoformat()
                    if last_success
                    else None,
                    "updated_at": datetime.fromtimestamp(updated, BEIJING).isoformat() if updated else None,
                    "error": error,
                    "retry_wait_seconds": retry_wait,
                    "retry_at": retry_at,
                    "failures": task["failures"] if task else 0,
                    "stage": stage,
                    "included": not paused and eligible and online,
                    "group": "daily" if kind == "products" else "core",
                }
            )
        included = [m for m in modules if m["included"]]
        done = sum(m["today_updated"] for m in included)
        states = {m["status"] for m in included}
        status = next(
            (
                s
                for s in (
                    "offline",
                    "stopped",
                    "login",
                    "error",
                    "waking",
                    "running",
                    "queued",
                    "retry_wait",
                    "deferred",
                )
                if s in states
            ),
            "updated" if included and done == len(included) else "waiting" if included else "unsupported",
        )
        core = [m for m in included if m["group"] == "core"]
        daily = [m for m in included if m["group"] == "daily"]
        if core and all(m["today_updated"] for m in core) and status == "waiting":
            status = "updated"
        if not included:
            status = (
                "paused"
                if modules and all(m["status"] == "paused" for m in modules)
                else "offline"
                if not online
                else "stopped"
                if item.state == "stopped"
                else "login"
                if item.state not in ("logged_in", "sleeping")
                else "unsupported"
            )
        rows.append(
            {
                **identity(shop),
                "shop_id": shop["id"],
                "status": status,
                "updated": sum(m["today_updated"] for m in core),
                "total": len(core),
                "overdue_modules": sum(m["overdue"] for m in core),
                "last_collected": max((m["last_collected"] for m in modules if m["last_collected"]), default=None),
                "daily_updated": sum(m["today_updated"] for m in daily),
                "daily_total": len(daily),
                "modules": modules,
                "last_updated": max((m["updated_at"] for m in modules if m["updated_at"]), default=None),
            }
        )
    return {
        "date": day,
        "timezone": "Asia/Shanghai",
        "collector_online": online,
        "rows": rows,
        "updated_shops": sum(r["total"] > 0 and r["updated"] == r["total"] for r in rows),
        "total_shops": sum(r["total"] > 0 for r in rows),
        "overdue_modules": sum(r["overdue_modules"] for r in rows),
        "last_collected": max((r["last_collected"] for r in rows if r["last_collected"]), default=None),
        "updated_modules": sum(r["updated"] for r in rows),
        "total_modules": sum(r["total"] for r in rows),
        "daily_updated": sum(r["daily_updated"] for r in rows),
        "daily_total": sum(r["daily_total"] for r in rows),
        "excluded_shops": sum(r["total"] == 0 for r in rows),
        "last_updated": max((r["last_updated"] for r in rows if r["last_updated"]), default=None),
    }
