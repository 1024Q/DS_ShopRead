"""Queue sequence numbers and rough start-time estimates for queued collection steps.

Read-only: the estimate replays the same admission limits as ``select_jobs``
(node capacity, per-shop concurrency, active-shop limit, one running step per
shop business module) over the current queue order. Durations come from an
in-memory moving average of recently finished steps, so the numbers are a guide,
not a promise: platform latency, retries and login checks shift them.
"""

import heapq
import time

DEFAULT_SECONDS = 30.0
MIN_REMAINING = 3.0
ALPHA = 0.3
MAX_SIMULATED = 2000


def namespace(service):
    value = getattr(service, "queue_namespace", None)
    if value is None:
        import uuid

        store = getattr(service, "store", None)
        value = None
        if store is not None and callable(getattr(store, "connect", None)):
            with store.connect() as db:
                row = db.execute("SELECT value FROM settings WHERE key='insights_identity'").fetchone()
                value = row[0] if row else None
        service.queue_namespace = value or str(uuid.uuid4())
    return service.queue_namespace


def step_key(sid, kind, scope):
    from .frequency_policy import task_id

    return sid, task_id(kind, scope or {})


SAVE_INTERVAL = 120
FILE_NAME = "queue-durations.json"


def _path(service):
    store = getattr(service, "store", None)
    directory = getattr(store, "directory", None)
    return directory / FILE_NAME if directory is not None else None


def durations(service):
    """Per shop/sub-step averages, loaded once from disk so estimates survive restarts."""
    values = getattr(service, "step_durations", None)
    if values is None:
        values = service.step_durations = {}
        path = _path(service)
        if path is not None:
            from .collector_update import read_json

            saved = read_json(path, {})
            for key, seconds in (saved.get("durations") or {}).items():
                sid, _, step = key.partition("|")
                if sid.isdigit() and step and isinstance(seconds, (int, float)) and 0 < seconds <= 86400:
                    values[int(sid), step] = float(seconds)
    return values


def save(service, force=False):
    path = _path(service)
    if path is None:
        return
    now = time.monotonic()
    if not force and now - getattr(service, "step_durations_saved", -SAVE_INTERVAL) < SAVE_INTERVAL:
        return
    service.step_durations_saved = now
    from .collector_update import write_json

    try:
        write_json(
            path,
            {
                "version": 1,
                "durations": {f"{sid}|{step}": round(v, 2) for (sid, step), v in durations(service).items()},
            },
        )
    except OSError:
        pass  # estimates only; never disturb collection


def record(service, sid, kind, scope, seconds):
    """Moving average of real read durations per shop/sub-step, saved every few minutes."""
    if not isinstance(seconds, (int, float)) or seconds <= 0 or seconds > 86400:
        return
    values = durations(service)
    key = step_key(sid, kind, scope)
    previous = values.get(key)
    values[key] = seconds if previous is None else previous + ALPHA * (seconds - previous)
    save(service)


def expected(service, sid, meta, config):
    known = durations(service)
    kind = meta.get("kind")
    value = known.get(step_key(sid, kind, meta.get("scope", {})))
    if value is None:
        # Same step in another shop is a better guess than a constant.
        from .frequency_policy import task_id

        step = task_id(kind, meta.get("scope", {}))
        peers = [v for (_, s), v in known.items() if s == step]
        value = sum(peers) / len(peers) if peers else None
    if value is None:
        from .frequency_policy import group

        g = (
            group(kind, meta.get("scope", {}), config, meta.get("platform", "pdd"))
            if config.get("frequency_version")
            else None
        )
        timeout = (g or {}).get("timeout") or 0
        value = min(DEFAULT_SECONDS, timeout / 2) if timeout else DEFAULT_SECONDS
    return max(1.0, float(value))


def order_key(meta, config, now):
    from .demand_refresh import order

    p = order(meta["kind"], meta.get("scope", {}), meta, config, meta.get("platform", "pdd"), now)
    return (meta.get("shop_turn_order", 0), p[0], p[1], p[2])


def estimates(service, now=None):
    """{job_key: {queue_position, queue_total, wait_seconds, expected_seconds}} for queued jobs."""
    from .collection_queue import scheduling_config, occupies_slot
    from .frequency_policy import limit

    now = time.time() if now is None else now
    config = scheduling_config(service)
    progress = dict(service.progress)
    capacity = max(1, int(config.get("max_concurrent") or 1))
    max_shops = max(1, int(config.get("max_active_shops", 2) or 2))
    running = []  # heap of (end_time, sid, kind)
    busy = {}
    modules = set()
    for key, meta in progress.items():
        if occupies_slot(meta):
            kind = meta.get("kind", key[1].split(":")[0])
            started = meta.get("started_at") or now
            remaining = max(MIN_REMAINING, expected(service, key[0], meta, config) - (now - started))
            heapq.heappush(running, (now + remaining, key[0], kind))
            busy[key[0]] = busy.get(key[0], 0) + 1
            modules.add((key[0], kind))
    queued = [
        (key, meta)
        for key, meta in progress.items()
        if meta.get("phase") == "queued" and not meta.get("slot_granted") and not meta.get("clearing_queue")
    ]
    queued.sort(key=lambda pair: (order_key(pair[1], config, now), pair[0]))
    total = len(queued)
    result = {}
    for position, (key, meta) in enumerate(queued, 1):
        result[key] = {"queue_position": position, "queue_total": total, "queue_number": f"{namespace(service)}/S{position}"}
    pending = queued[:MAX_SIMULATED]
    clock = now
    while pending:
        started_any = False
        for item in list(pending):
            key, meta = item
            sid = key[0]
            kind = meta.get("kind", key[1].split(":")[0])
            ready_at = max(meta.get("retry_at", 0) or 0, 0)
            if ready_at > clock or meta.get("blocked"):
                continue
            if len(running) >= capacity:
                break
            active = {s for s, n in busy.items() if n > 0}
            if sid not in active and len(active) >= max_shops:
                continue
            if busy.get(sid, 0) >= limit(config, sid) or (sid, kind) in modules:
                continue
            seconds = expected(service, sid, meta, config)
            result[key].update(wait_seconds=int(round(clock - now)), expected_seconds=int(round(seconds)))
            heapq.heappush(running, (clock + seconds, sid, kind))
            busy[sid] = busy.get(sid, 0) + 1
            modules.add((sid, kind))
            pending.remove(item)
            started_any = True
        if not pending:
            break
        if started_any and len(running) < capacity:
            continue  # freed limits may admit more at the same instant
        if running:
            end, sid, kind = heapq.heappop(running)
            clock = max(clock, end)
            busy[sid] -= 1
            modules.discard((sid, kind))
            continue
        # Nothing running and nothing startable: wait for the earliest retry window.
        future = [
            m.get("retry_at", 0) for _, m in pending if (m.get("retry_at", 0) or 0) > clock and not m.get("blocked")
        ]
        if not future:
            break
        clock = min(future)
    for key, value in result.items():
        value.setdefault("wait_seconds", None)
    return result


def describe(seconds):
    """Short human text for a wait estimate."""
    if seconds is None:
        return "暂无法估算"
    if seconds < 5:
        return "即将开始"
    if seconds < 60:
        return f"约 {int(seconds)} 秒"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"约 {minutes} 分 {rest} 秒" if rest and minutes < 10 else f"约 {minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    return f"约 {hours} 小时 {minutes} 分钟"
