from .task_timing import fields as timing_fields, text as timing_text
from .master_tasks import label as task_title
from .read_budget import remaining as read_remaining
from .shop_identity import identity
from .platform_support import supports

"""Read-only synchronization metadata; polling never starts browser collection."""
import json
import time
from datetime import datetime
from .business import KINDS
from .business_reader import BEIJING
from . import collection
from .collection_queue import queue_status, waiting_reason, queue_for, blocking_task
from .collection_priority import level as priority_level


def summary(service, task_group=None, page=1):
    """Header polling reads identifiers only, never business JSON or task details."""
    records = service.store.list()
    cached = {}
    ledger = []
    from .status_cache import get as cached_summary

    with service.store.connect() as db:
        for shop in records:
            sid = shop["id"]

            def load(sid=sid):
                # Keep aggregation in the individual shop database. A node-wide
                # UNION is federated through an in-memory SQLite copy of every
                # cache header; periodic status polling must not materialize it.
                kinds = {
                    row["kind"]: row["updated"]
                    for row in db.execute(
                        """SELECT CASE WHEN cache_key LIKE 'summary:transactions:%' THEN 'transactions'
                WHEN cache_key LIKE 'summary:ad_reports:%' THEN 'ad_reports'
                WHEN instr(cache_key,':')>0
                THEN substr(cache_key,1,instr(cache_key,':')-1) ELSE cache_key END AS kind,
                max(updated) AS updated FROM (
                    SELECT cache_key,updated FROM business_cache WHERE shop_id=?
                    UNION ALL SELECT cache_key,updated FROM daily_archive WHERE shop_id=?)
                GROUP BY kind""",
                        (sid, sid),
                    )
                }
                rows = [
                    dict(r)
                    for r in db.execute(
                        "SELECT shop_id,cache_key,kind,last_requested,retry_at,last_error,continuation FROM collection_tasks WHERE shop_id=? AND schedule_state<>'archived'",
                        (sid,),
                    )
                ]
                return kinds, rows

            # Rebuilt only after a write touched this shop (or a short TTL).
            kinds, rows = cached_summary(service.store, sid, load)
            for kind, updated in kinds.items():
                cached[(sid, kind)] = updated
            ledger.extend(dict(r) for r in rows)
    latest = {}
    for r in sorted(ledger, key=lambda r: max(r["last_requested"], r["retry_at"])):
        group = (r["shop_id"], r["kind"])
        if r["last_error"] and r["retry_at"] > cached.get(group, 0):
            latest[group] = r["last_error"]
    active_keys = {(r["shop_id"], r["cache_key"]) for r in ledger}
    for key, (stamp, message) in service.errors.items():
        group = (key[0], key[1].split(":")[0])
        if key in active_keys and stamp > cached.get(group, 0):
            latest[group] = message
    for key in service.progress:
        latest.pop((key[0], key[1].split(":")[0]), None)
    waiting = 0
    waiting_groups = []
    for shop in records:
        if service.session_getter(shop["id"]).state == "stopped":
            latest = {key: value for key, value in latest.items() if key[0] != shop["id"]}
        if service.session_getter(shop["id"]).state == "needs_verification":
            waiting_groups.extend((shop["id"], kind) for kind in KINDS if supports(shop.get("platform", "pdd"), kind))
            waiting = len(waiting_groups)
    if task_group:
        from .collector_task_list import listing

        return listing(service, task_group, page, cached, latest, waiting_groups)
    from .log_spool import status as log_status
    from .center.replication_worker import status as delivery_status
    from .collector_task_list import deferred_categories

    categories = deferred_categories(service)
    from .master_tasks import project, counts, enabled

    masters = project(service)
    totals = counts(masters)
    return {
        "deferred_categories": categories,
        "dispatch": getattr(service, 'dispatch_snapshot', {}),
        "database_write": service.store.write_status(),
        "delivery": delivery_status(service.store.directory),
        "log_ingestion": log_status(service.store),
        "execution_steps": {
            phase: sum(p.get("phase") == phase for p in service.progress.values()) for phase in ("running", "queued")
        },
        "summary": {
            "running": totals["running"]
            if enabled(service.queue_config)
            else sum(p.get("phase") == "running" for p in service.progress.values()),
            "queued": totals["queued"]
            if enabled(service.queue_config)
            else sum(p.get("phase") == "queued" for p in service.progress.values()),
            "errors": len(latest),
            "waiting_login": waiting,
            "cached": len(cached),
            "deferred": sum(categories.values()),
        },
        "collection": collection.settings(service.store),
        "timezone": "Asia/Shanghai",
    }


LEDGER_COLUMNS = "shop_id,cache_key,kind,options,last_requested,retry_at,failures,last_error,schedule_state,continuation,error_kind,blocked,adaptive_seconds"


def attention_counts(store, db):
    """Row counts per attention snapshot, recomputed only for snapshots whose version changed."""
    # The trigger-maintained sidecar may be missing (old schema) or lack a row (legacy): fall back to the payload.
    stats = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attention_snapshot_stats'").fetchone()
    source = (
        "attention_snapshots a LEFT JOIN attention_snapshot_stats t USING(shop_id,kind)"
        if stats
        else "attention_snapshots a"
    )
    expression = (
        "coalesce(t.row_count,json_array_length(a.payload,'$.rows'))"
        if stats
        else "json_array_length(a.payload,'$.rows')"
    )
    old = getattr(store, "_attention_counts", {})
    current = {}
    headers = [
        r
        for shop in store.list()
        for r in db.execute("SELECT shop_id,kind,updated FROM attention_snapshots WHERE shop_id=?", (shop["id"],))
    ]
    for r in headers:
        key = (r["shop_id"], r["kind"], r["updated"])
        if key in old:
            current[key] = old[key]
            continue
        row = db.execute(
            f"SELECT a.updated,{expression} AS loaded_count FROM {source} WHERE a.shop_id=? AND a.kind=?",
            (r["shop_id"], r["kind"]),
        ).fetchone()
        if row:
            current[(r["shop_id"], r["kind"], row["updated"])] = row["loaded_count"]
    store._attention_counts = current
    return {
        (sid, kind): {"shop_id": sid, "kind": kind, "updated": updated, "loaded_count": count}
        for (sid, kind, updated), count in current.items()
    }


def progress_snapshot(service):
    """Queue/progress view without the per-shop x per-kind source details."""
    return snapshot(service, sources=False)


def snapshot(service, sources=True):
    now = time.time()
    store = service.store
    config = collection.settings(store)
    records = store.list()
    with store.connect() as db:
        # Scan small identifiers every poll; decode large cached payloads only when updated.
        headers = []
        for shop in records:
            headers.extend(
                dict(r)
                for r in db.execute(
                    "SELECT 'business_cache' AS origin,shop_id,cache_key,updated FROM business_cache WHERE shop_id=? UNION ALL SELECT 'daily_archive',shop_id,cache_key,updated FROM daily_archive WHERE shop_id=?",
                    (shop["id"], shop["id"]),
                )
            )
        headers.sort(key=lambda r: r["updated"], reverse=True)
        old = getattr(store, "_status_metadata", {})
        metadata = {}
        for origin in ("business_cache", "daily_archive"):
            changed = [
                r
                for r in headers
                if r["origin"] == origin and (origin, r["shop_id"], r["cache_key"], r["updated"]) not in old
            ]
            grouped = {}
            for row in changed:
                grouped.setdefault(row["shop_id"], []).append(row)
            for sid, shop_rows in grouped.items():
                for offset in range(0, len(shop_rows), 100):
                    batch = shop_rows[offset : offset + 100]
                    marks = ",".join("(?,?,?)" for _ in batch)
                    params = [v for r in batch for v in (r["shop_id"], r["cache_key"], r["updated"])] + [sid]
                    for row in db.execute(
                        f"""WITH wanted(shop_id,cache_key,updated) AS (VALUES {marks})
                        SELECT c.shop_id,c.cache_key,c.updated,
                        coalesce(t.row_count,json_array_length(c.payload,'$.rows')) AS loaded_count,
                        json_extract(coalesce(t.metadata,c.payload),'$.source_total') AS source_total,
                        json_extract(coalesce(t.metadata,c.payload),'$.complete') AS complete,
                        json_extract(coalesce(t.metadata,c.payload),'$.summary_complete') AS summary_complete,
                        json_extract(coalesce(t.metadata,c.payload),'$.details_on_demand') AS details_on_demand,
                        json_extract(coalesce(t.metadata,c.payload),'$.source_unit') AS source_unit,
                        json_extract(coalesce(t.metadata,c.payload),'$.metrics.source_time') AS source_time,
                        json_extract(coalesce(t.metadata,c.payload),'$.latest_summary_day') AS latest_summary_day,
                        json_extract(coalesce(t.metadata,c.payload),'$._sync.scope') AS scope,
                        json_extract(coalesce(t.metadata,c.payload),'$._sync.duration_seconds') AS duration_seconds,
                        json_extract(coalesce(t.metadata,c.payload),'$._sync.collection_mode') AS collection_mode,
                        json_extract(coalesce(t.metadata,c.payload),'$._sync.full_at') AS full_at,
                        json_extract(coalesce(t.metadata,c.payload),'$._order_stream') AS order_stream,
                        json_extract(coalesce(t.metadata,c.payload),'$._order_revision') AS order_revision
                        FROM {origin} c JOIN wanted w USING(shop_id,cache_key,updated) LEFT JOIN table_snapshots t ON t.shop_id=c.shop_id AND t.cache_key=c.cache_key AND t.updated=c.updated WHERE c.shop_id=?""",
                        params,
                    ):
                        metadata[(origin, row["shop_id"], row["cache_key"], row["updated"])] = dict(row)
        cached = []
        for row in headers:
            key = (row["origin"], row["shop_id"], row["cache_key"], row["updated"])
            value = metadata.get(key) or old.get(key)
            # A collector can publish between the header and payload reads.
            # Retry that version on the next poll instead of caching an empty row.
            if value and "shop_id" in value:
                metadata[key] = value
                cached.append(value)
        store._status_metadata = metadata
        from .source_groups import summary_metadata

        cached = summary_metadata(cached)
        # Per-shop reads: an unscoped query would federate every shop database.
        ledger = {}
        checkpoints = {}
        for shop in records:
            for r in db.execute(f"SELECT {LEDGER_COLUMNS} FROM collection_tasks WHERE shop_id=?", (shop["id"],)):
                ledger[(r["shop_id"], r["cache_key"])] = dict(r)
            for r in db.execute(
                "SELECT shop_id,cache_key,loaded,total,updated FROM collection_checkpoints WHERE shop_id=?",
                (shop["id"],),
            ):
                checkpoints[(r["shop_id"], r["cache_key"])] = dict(r)
    with store.connect() as db:
        urgent = attention_counts(store, db)
    for (sid, key), task in ledger.items():
        if json.loads(task["options"]).get("_attention") and (sid, task["kind"]) in urgent:
            row = urgent[(sid, task["kind"])]
            cached.append(
                {
                    **row,
                    "cache_key": key,
                    "scope": task["options"],
                    "complete": True,
                    "source_total": row["loaded_count"],
                    "collection_mode": "attention",
                }
            )
    # Materialized order query views are not browser collection scopes.
    order_views = {}
    for r in cached:
        if r["cache_key"].startswith("orders:view-v2:"):
            order_views.setdefault(r["shop_id"], r)
    cached = [r for r in cached if not r["cache_key"].startswith("orders:view-v")]
    cached.sort(key=lambda r: r.get("updated", 0), reverse=True)
    by_key = {}
    for r in cached:
        by_key.setdefault((r["shop_id"], r["cache_key"]), r)
    from .source_groups import source_groups

    deferred = getattr(service, "deferred", {})
    platforms = {s["id"]: s.get("platform", "pdd") for s in records}
    latest, group_for = source_groups(
        by_key, ledger, checkpoints, service.progress, service.errors, deferred, config, platforms
    )
    pending_by_group = {group_for(key): v for key, v in deferred.items()}
    from .center.source_delivery import load as load_delivery, describe as describe_delivery

    delivery_saved = load_delivery(store.directory) if sources else {}
    rows = []
    for shop in records:
        item = service.session_getter(shop["id"])
        groups = sorted((kind, q) for sid, kind, q in latest if sid == shop["id"])
        present = {kind for kind, q in groups}
        groups.extend(
            (kind, queue_for({"kind": kind, "scope": {}, "platform": shop.get("platform", "pdd")}, config))
            for kind in sorted(KINDS)
            if kind not in present
        )
        for kind, queue in groups:
            if not supports(shop.get("platform", "pdd"), kind):
                continue
            key = latest.get((shop["id"], kind, queue))
            cache = by_key.get(key, {})
            progress = service.progress.get(key, {})
            error = service.errors.get(key)
            waiting = (key in service.waiting_login and item.state != "logged_in") or item.state == "needs_verification"
            message = error[1] if error else ledger.get(key, {}).get("last_error") or None
            if ledger.get(key, {}).get("schedule_state") == "archived":
                message = None
                error = None
            if message and not error:
                error = (0, message)
            status = (
                "waiting_login"
                if waiting
                else progress.get("phase")
                or (
                    "error" if error else "missing" if not cache else "ready"
                    if cache.get("complete") or cache.get("summary_complete") and cache.get("details_on_demand")
                    else "partial"
                )
            )
            retrying = progress.get("phase") in ("queued", "running") and not waiting
            stopped = item.state == "stopped" and not retrying
            task = ledger.get(key, {})
            if task.get("blocked") and not retrying:
                status = "blocked"
            elif task.get("continuation") and not retrying and not error:
                status = "paused_batch"
            if stopped:
                status = "stopped"
            pending = pending_by_group.get((shop["id"], kind, queue))
            data_status = (
                "deferred"
                if pending and status not in ("running", "queued", "error", "blocked", "waiting_login")
                else status
            )
            if not sources:
                rows.append(
                    {
                        "shop_id": shop["id"],
                        "kind": kind,
                        "data_status": data_status,
                        "updated_at": True if cache else None,
                        "duration_seconds": cache.get("duration_seconds"),
                    }
                )
                continue
            scope = (
                progress.get("scope")
                or (deferred.get(key) or {}).get("scope")
                or (
                    json.loads(cache["scope"])
                    if cache.get("scope")
                    else json.loads(ledger.get(key, {}).get("options", "{}"))
                )
            )
            checkpoint = checkpoints.get(key, {})
            retry_enabled = (
                not stopped
                and not task.get("blocked")
                and config["enabled"]
                and task.get("schedule_state") != "archived"
                and collection.mode(kind, scope, config, shop.get("platform", "pdd")) != "off"
            )
            retry_at = task.get("retry_at") if message and retry_enabled and not retrying and not waiting else None
            rows.append(
                {
                    **identity(shop),
                    "shop_id": shop["id"],
                    "shop_name": shop["name"] or f"店铺 {shop['id']}",
                    "kind": kind,
                    "delivery": describe_delivery(
                        delivery_saved,
                        shop["id"],
                        key[1] if key else kind,
                        cache,
                        scope,
                        checkpoint,
                        order_views.get(shop["id"]),
                    ),
                    "cache_key": key[1] if key else None,
                    "state": item.state,
                    "deferred": bool(pending),
                    "data_status": data_status,
                    "syncing": key in service.jobs,
                    "waiting_login": waiting,
                    "updated_at": datetime.fromtimestamp(cache["updated"], BEIJING).isoformat() if cache else None,
                    "source_time": cache.get("source_time") or cache.get("latest_summary_day"),
                    "loaded_count": cache.get("loaded_count") or 0,
                    "source_total": cache.get("source_total"),
                    "source_unit": cache.get("source_unit") or "记录",
                    "complete": bool(cache.get("complete")),
                    "summary_complete": bool(cache.get("summary_complete")),
                    "details_pending": bool(cache.get("details_pending")),
                    "details_on_demand": bool(cache.get("details_on_demand")),
                    "details_complete": bool(cache.get("details_complete", cache.get("complete"))),
                    "summary_updated_at": cache.get("summary_updated_at"),
                    "details_updated_at": cache.get("details_updated_at", cache.get("updated")),
                    "stale": bool(
                        cache
                        and (
                            collection.due(
                                kind,
                                scope,
                                cache["updated"],
                                config,
                                now,
                                platform=shop.get("platform", "pdd"),
                                adaptive_seconds=ledger.get(key, {}).get("adaptive_seconds", 0),
                            )
                            or error
                            or item.state not in ("logged_in", "sleeping")
                        )
                    ),
                    "error": None if retrying or stopped else message,
                    "previous_error": message if retrying or stopped else None,
                    "failures": task.get("failures", 0),
                    "retry_at": retry_at,
                    "error_kind": task.get("error_kind"),
                    "blocked": bool(task.get("blocked")),
                    "continuation": bool(task.get("continuation")),
                    "checkpoint_loaded": checkpoint.get("loaded", 0),
                    "checkpoint_total": checkpoint.get("total"),
                    "age_seconds": max(0, int(now - cache["updated"])) if cache else None,
                    "retry_wait_seconds": max(0, round(retry_at - now)) if retry_at else None,
                    "retry_enabled": retry_enabled,
                    "sync_phase": progress.get("phase"),
                    "sync_stage": (progress.get("stage", "") + " · " + timing_text(progress))
                    if progress.get("phase") == "running" and timing_text(progress)
                    else progress.get("stage"),
                    "started_at": progress.get("started_at"),
                    "timeout_seconds": progress.get("timeout_seconds"),
                    "remaining_seconds": read_remaining(progress, now),
                    "wait_seconds": round(now - progress.get("queued_at", now))
                    if progress.get("phase") == "queued"
                    else 0,
                    **timing_fields(progress, now),
                    "wait_reason": waiting_reason(key, progress, service.progress, config)
                    + (
                        " · " + timing_text(progress)
                        if progress.get("phase") == "queued" and timing_text(progress)
                        else ""
                    ),
                    "collection_mode": cache.get("collection_mode"),
                    "full_checked_at": datetime.fromtimestamp(cache["full_at"], BEIJING).isoformat()
                    if cache.get("full_at")
                    else None,
                    "sync_loaded": progress.get("loaded", 0),
                    "sync_total": progress.get("total"),
                    "elapsed_seconds": round(now - progress["started_at"], 1) if progress.get("started_at") else None,
                    "duration_seconds": cache.get("duration_seconds"),
                    "priority": priority_level(kind, scope, config, shop.get("platform", "pdd")),
                    "queue_names": {g["id"]: g["name"] for g in config.get("frequency_groups", [])},
                    "scope": scope,
                    "queue_entries": [{"queue": queue}],
                    "queues": [queue],
                    "queue": queue,
                    "cadence": collection.mode(kind, scope, config, shop.get("platform", "pdd")),
                    "task_label": task_title(kind, scope, config, shop.get("platform", "pdd")),
                    "frequency_name": collection.frequency_name(kind, scope, config, shop.get("platform", "pdd")),
                    "custom_interval": collection.uses_custom_interval(kind, config, shop.get("platform", "pdd"))
                    and collection.mode(kind, scope, config, shop.get("platform", "pdd")) == "realtime",
                    "interval_seconds": collection.effective_interval(
                        kind, scope, config, shop.get("platform", "pdd"), ledger.get(key, {}).get("adaptive_seconds", 0)
                    )
                    if collection.mode(kind, scope, config, shop.get("platform", "pdd")) == "realtime"
                    else None,
                }
            )
    shops = {s["id"]: s for s in records}
    failures = {}
    for (sid, key), task in ledger.items() if sources else ():
        if task["last_error"] and task["schedule_state"] != "archived" and (sid, key) not in service.jobs:
            scope = json.loads(task["options"])
            shop = shops.get(sid, {})
            enabled = (
                config["enabled"]
                and not task["blocked"]
                and collection.mode(task["kind"], scope, config, shop.get("platform", "pdd")) != "off"
            )
            failures.setdefault(group_for((sid, key)), []).append(
                {
                    "cache_key": key,
                    "scope": scope,
                    "error_kind": task["error_kind"],
                    "error": task["last_error"],
                    "failures": task["failures"],
                    "retry_at": task["retry_at"] if enabled else None,
                    "blocked": bool(task["blocked"]),
                    "retry_enabled": enabled,
                }
            )
    for row in rows if sources else ():
        row["failed_scopes"] = failures.get((row["shop_id"], row["kind"], row["queue"]), [])
        from .collection_errors import guidance

        for failure in row["failed_scopes"]:
            failure["repair"] = guidance(
                row["kind"], failure["error"], failure.get("error_kind", ""), failure.get("blocked", False)
            )
        row["repair_advice"] = list(
            {(f["repair"]["reason"], f["repair"]["action"]): f["repair"] for f in row["failed_scopes"]}.values()
        )
        row["failed_scope_failures"] = max((f["failures"] for f in row["failed_scopes"]), default=0)
        deadlines = [f["retry_at"] for f in row["failed_scopes"] if f.get("retry_at") is not None]
        row["repair_retry_at"] = min(deadlines) if deadlines else None
        row["repair_auto_count"] = len(deadlines)
        row["repair_manual_count"] = sum(bool(f.get("blocked")) for f in row["failed_scopes"])
        row["retry_server_time"] = now
    durations = {(r["shop_id"], r["kind"]): r["duration_seconds"] for r in rows}
    from .demand_refresh import timing

    tasks = [
        {
            **timing(
                m.get("kind", k[1].split(":")[0]),
                m.get("scope", {}),
                m,
                config,
                shops[k[0]].get("platform", "pdd"),
                now,
            ),
            **identity(shops[k[0]]),
            "shop_id": k[0],
            "kind": m.get("kind", k[1].split(":")[0]),
            "phase": m["phase"],
            "scope": m.get("scope", {}),
            "priority": priority_level(
                m.get("kind", k[1].split(":")[0]), m.get("scope", {}), config, shops[k[0]].get("platform", "pdd")
            ),
            "queue": queue_for({"kind": m.get("kind", k[1].split(":")[0]), **m}, config),
            "frequency_name": collection.frequency_name(
                m.get("kind", k[1].split(":")[0]), m.get("scope", {}), config, shops[k[0]].get("platform", "pdd")
            ),
            "started_at": m.get("started_at"),
            "timeout_seconds": m.get("timeout_seconds"),
            "remaining_seconds": read_remaining(m, now),
            "wait_seconds": round(now - m.get("queued_at", now)) if m["phase"] == "queued" else 0,
            "elapsed_seconds": round(now - m["started_at"]) if m.get("started_at") else 0,
            **timing_fields(m, now),
            "wait_reason": waiting_reason(k, m, service.progress, config),
            "blocking_task": blocking_task(k, m, service.progress, now),
            "stage": m.get("stage"),
            "retry": bool(m.get("failures")),
        }
        for k, m in service.progress.items()
        if k[0] in shops
    ]
    tasks.sort(key=lambda r: (r["phase"] != "running", -r["wait_seconds"]))
    for row in tasks:
        blocker = row.get("blocking_task")
        row["blocking_kind"] = blocker.get("kind") if blocker else None
        row["last_duration_seconds"] = durations.get((row["shop_id"], row["kind"]))
        row["priority_until"] = getattr(service, "shop_priorities", {}).get(row["shop_id"], 0)
    finished = [r for r in getattr(service, "completions", []) if r[0] > now - 900]
    minutes = max(1, min(900, now - getattr(service, "started_at", now)) / 60)
    diagnostics = {
        "completed_15m": len(finished),
        "success_15m": sum(r[1] for r in finished),
        "failed_15m": sum(not r[1] for r in finished),
        "tasks_per_minute": round(len(finished) / minutes, 1),
        "sample_minutes": round(minutes, 1),
        "merged_demands": len(getattr(getattr(service, "demands", None), "merged", {})),
        "deferred_count": len(deferred),
        "queue_caps": getattr(service, "queue_caps", {}),
        "slow_sources": sum((r.get("duration_seconds") or 0) > 60 for r in rows),
        "archived_tasks": sum(t.get("schedule_state") == "archived" for t in ledger.values()),
        "tasks": tasks,
    }
    from .center.replication_worker import status as delivery_status

    result = {
        "sources": rows,
        "delivery": delivery_status(store.directory),
        "machine": getattr(getattr(service, "load_controller", None), "status", {}),
        "queues": queue_status(service.progress, {**config, **getattr(service, "runtime_limits", {})}),
        "diagnostics": diagnostics,
        "summary": {
            "running": sum(p.get("phase") == "running" for p in service.progress.values()),
            "queued": sum(p.get("phase") == "queued" for p in service.progress.values()),
            "errors": sum(r["data_status"] in ("error", "blocked") for r in rows),
            "waiting_login": sum(r["data_status"] == "waiting_login" for r in rows),
            "cached": sum(bool(r["updated_at"]) for r in rows),
            "deferred": len(deferred),
        },
        "timezone": "Asia/Shanghai",
    }
    from .master_tasks import project, counts, enabled

    if enabled(config):
        masters = [r for r in project(service) if r["shop_id"] in shops]
        for row in masters:
            row.update(identity(shops[row["shop_id"]]))
        result["master_tasks"] = masters
        result["execution_steps"] = {
            phase: sum(t.get("phase") == phase for (sid, _), t in service.progress.items() if sid in shops)
            for phase in ("running", "queued")
        }
        totals = counts(masters)
        result["summary"].update(
            running=totals["running"], queued=totals["queued"], deferred=totals["deferred"] + totals["waiting"]
        )
        result["diagnostics"]["step_count"] = len(tasks)
        result["diagnostics"]["master_count"] = len(masters)
    if not sources:
        result.pop("sources")
    result['dispatch'] = getattr(service, 'dispatch_snapshot', {})
    return result
