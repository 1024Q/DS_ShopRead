"""Refresh pending demand without replacing an active read or its checkpoint."""

from . import collection


def timing(kind, scope, state, config, platform, now):
    from datetime import datetime, timedelta
    from .business_reader import BEIJING

    since = state.get("waiting_since") or state.get("queued_at") or now
    seconds = max(1, collection.interval_seconds(kind, config, platform, scope))
    if collection.mode(kind, scope, config, platform) == "daily":
        from .frequency_policy import group

        g = group(kind, scope, config, platform) if config.get("frequency_version") else None
        hour, minute = map(int, (g.get("time") if g else config.get("daily_time", "03:00")).split(":"))
        deadline = datetime.fromtimestamp(now if state.get("last_success") else since, BEIJING).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if deadline.timestamp() <= (state.get("last_success") or since):
            deadline += timedelta(days=1)
        deadline = deadline.timestamp()
        seconds = 86400
    else:
        deadline = (state.get("last_success") or since) + seconds
    return {
        "generated_at": since,
        "last_merged_at": state.get("last_merged_at"),
        "wait_seconds": max(0, int((state.get("started_at") or now) - since)),
        "queue_deadline": deadline,
        "overdue_seconds": max(0, int(now - deadline)) if state.get("phase") != "running" else 0,
        "queue_interval_seconds": seconds,
    }


def order(kind, scope, state, config, platform, now):
    # A historical batch joins a new queue turn after saving its checkpoint.
    # Keep the original generation time for diagnostics and range merging.
    if state.get("continuation") and state.get("queue_turn_at"):
        state = {
            **state,
            "waiting_since": state["queue_turn_at"],
            "queued_at": state["queue_turn_at"],
            "last_success": 0,
        }
    t = timing(kind, scope, state, config, platform, now)
    overdue = max(0, now - t["queue_deadline"])
    return (0 if now >= t["queue_deadline"] else 1, -overdue / t["queue_interval_seconds"], t["generated_at"])


def overdue(kind, scope, state, config, platform, now):
    t = timing(kind, scope, state, config, platform, now)
    return 0 if now < t["queue_deadline"] else 1 + int((now - t["queue_deadline"]) / t["queue_interval_seconds"])


def refresh(meta, scope, now, config):
    if meta.get("phase") != "queued":
        return
    periods = overdue(meta["kind"], meta.get("scope", {}), meta, config, meta.get("platform", "pdd"), now)
    if periods > meta.get("merged_periods", 0):
        meta["last_merged_at"] = now
    meta["merged_periods"] = max(meta.get("merged_periods", 0), periods)
    # A continuation/retry owns a persisted pagination scope. Never invalidate it.
    if meta.get("continuation") or meta.get("failures"):
        return
    old = meta.get("scope", {})
    merged = {**scope}
    for key in ("start", "date_from"):
        if old.get(key) is not None and scope.get(key) is not None:
            merged[key] = min(old[key], scope[key])
    for key in ("end", "date_to"):
        if old.get(key) is not None and scope.get(key) is not None:
            merged[key] = max(old[key], scope[key])
    meta["scope"] = merged
