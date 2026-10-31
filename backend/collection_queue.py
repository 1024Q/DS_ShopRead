"""Per-machine admission control with reserved queues and per-shop limits."""

import time, math
from . import collection

QUEUES = ("focus", "normal")
# Persist legacy field names so installed node settings and rollback stay compatible.
CONCURRENCY_FIELDS = ("max_concurrent", "shop_concurrent", "shop_limits", "max_active_shops", "browser_idle_seconds")


def scheduling_config(service):
    """Use the same reserved shop view for admission and displayed wait reasons."""
    config = {**service.queue_config, **getattr(service, "runtime_limits", {})}
    rotation = getattr(service, "shop_rotation", None)
    config["_rotation_shops"] = list(rotation.rounds) if rotation else []
    config["_waking_shops"] = list({k[0] for k, m in service.progress.items() if m.get("browser_waking")})
    return config


DUPLICATE_STAGE = "同一子步骤已在队列或执行中，本轮不重复生成"


def active_steps(progress):
    """(shop, sub-step id) -> job key of every queued or running job."""
    from .frequency_policy import task_id

    result = {}
    for key, meta in list(progress.items()):
        if meta.get("phase") in ("queued", "running"):
            kind = meta.get("kind", key[1].split(":")[0])
            result.setdefault((key[0], task_id(kind, meta.get("scope", {}))), key)
    return result


def duplicate_step(progress, job_key, kind, scope, steps=None):
    """Return the existing job that already covers this sub-step, if any.

    A scheduled demand whose sub-step (for example orders@today) is still queued
    or running is not generated again, even when its scope/cache key differs, so
    an overdue task never stacks copies of itself in the queue.
    """
    from .frequency_policy import task_id

    steps = active_steps(progress) if steps is None else steps
    owner = steps.get((job_key[0], task_id(kind, scope)))
    return owner if owner is not None and owner != job_key else None


def occupies_slot(meta):
    return meta.get("phase") == "running" or bool(meta.get("slot_granted"))


def reservations(config):
    if config.get("frequency_version"):
        return {g["id"]: 0 for g in config["frequency_groups"]}
    return {
        "focus": config["realtime_concurrent"],
        "normal": config["daily_concurrent"],
        **({"daily": config["scheduled_concurrent"]} if config.get("queue_version") == 3 else {}),
    }


def queue_caps(config):
    return (
        {}
        if config.get("frequency_version")
        else {q: max(2, n * 2) for q, n in reservations(config).items()}
        if config.get("queue_version") == 3
        else {"focus": max(2, config["max_concurrent"] * 2), "normal": max(4, config["max_concurrent"] * 3)}
    )


def queue_for(meta, config):
    if config.get("frequency_version"):
        from .frequency_policy import selected

        return (
            meta.get("queue")
            if meta.get("phase") == "running" and meta.get("queue")
            else selected(meta.get("kind"), meta.get("scope", {}), config, meta.get("platform", "pdd"))
        )
    if meta.get("phase") == "running" and meta.get("queue") in reservations(config):
        return meta["queue"]
    if config.get("queue_version") == 3:
        selected = collection.module_mode(meta.get("kind"), config, meta.get("platform", "pdd"))
        return "focus" if selected == "realtime" else "daily" if selected == "daily" else "normal"
    # Backfill is always idle work; moving a module must not promote history.
    if meta.get("scope", {}).get("_order_lane") in ("history", "daily"):
        return "normal"
    selected = config.get("platform_queues", {}).get(meta.get("platform", "pdd"), {}).get(meta.get("kind"))
    if selected in QUEUES:
        return selected
    return "focus" if meta.get("scope", {}).get("_attention") else "normal"


def select_jobs(progress, config, focus_kind=None, now=None):
    from .refund_priority import expedited
    now = time.time() if now is None else now
    running = [(key, meta) for key, meta in progress.items() if occupies_slot(meta)]
    available = max(0, config["max_concurrent"] - len(running))
    if not available:
        return []
    from collections import Counter

    busy = Counter(key[0] for key, _ in running)
    from .frequency_policy import limit

    active_modules = {(k[0], m["kind"]) for k, m in running}
    counts = {q: sum(queue_for(m, config) == q for _, m in running) for q in reservations(config)}
    retries = sum(bool(m.get("failures")) for _, m in running)

    def priority(pair):
        key, meta = pair
        from .demand_refresh import order

        return (*order(meta["kind"], meta.get("scope", {}), meta, config, meta.get("platform", "pdd"), now), key)

    candidates = sorted(
        (
            (key, m)
            for key, m in progress.items()
            if m.get("phase") == "queued"
            and not m.get("slot_granted")
            and not (m.get("shared_result_ready") and not m.get("force"))
            and not m.get("database_pressure")
            and not m.get("browser_blocked")
            and not m.get("blocked")
            and m.get("retry_at", 0) <= now
        ),
        key=lambda pair: (not expedited(pair[1]), *priority(pair)),
    )
    selected = []
    occupied = set(busy) | set(config.get("_rotation_shops", ()))
    while available:
        eligible = [
            (k, m)
            for k, m in candidates
            if k not in selected
            and (not m.get("shop_round_wait") or expedited(m))
            and (k[0] in occupied or len(occupied) < config.get("max_active_shops", 2))
            and (busy[k[0]] > 0 or sum(n > 0 for n in busy.values()) < config.get("max_active_shops", 2))
            and busy[k[0]] < limit(config, k[0])
            and (k[0], m["kind"]) not in active_modules
            and (
                config.get("frequency_version") or not m.get("failures") or retries < config.get("retry_concurrent", 1)
            )
        ]
        reserved = (
            []
            if config.get("frequency_version")
            else [
                (k, m) for k, m in eligible if counts[queue_for(m, config)] < reservations(config)[queue_for(m, config)]
            ]
        )
        pool = eligible if config.get("frequency_version") else reserved or (eligible if config["borrow_idle"] else [])
        if not pool:
            break
        if config.get("frequency_version"):
            # Round-robin shops within the same overdue interval, while older
            # overdue intervals always win. No module priority or extra quota.
            def fair(pair):
                p = priority(pair)
                return (not expedited(pair[1]), pair[1].get("shop_turn_order", 0), p[0], math.floor(p[1]), busy[pair[0][0]], p[2], pair[0])

            key, meta = min(pool, key=fair)
        else:
            key, meta = pool[0]
        queue = queue_for(meta, config)
        selected.append(key)
        active_modules.add((key[0], meta["kind"]))
        busy[key[0]] += 1
        counts[queue] = counts.get(queue, 0) + 1
        available -= 1
        occupied.add(key[0])
        retries += bool(meta.get("failures"))
    return selected


def queue_status(progress, config):
    result = {q: {"running": 0, "queued": 0, "reserved": n} for q, n in reservations(config).items()}
    # Queue capacity counts actual executions, not presentation master rows.
    for key, meta in list(progress.items()):
        meta = {"kind": key[1].split(":")[0], **meta}
        phase = meta.get("phase")
        if phase in ("running", "queued"):
            result.setdefault(queue_for(meta, config), {"running": 0, "queued": 0, "reserved": 0})[phase] += 1
    for row in result.values():
        row["borrowed"] = 0 if config.get("frequency_version") else max(0, row["running"] - row["reserved"])
    now = time.time()
    waits = [now - m["queued_at"] for m in progress.values() if m.get("phase") == "queued"]
    return {
        "max_concurrent": config["max_concurrent"],
        "max_active_shops": config.get("max_active_shops", 2),
        "running_shops": len({k[0] for k, m in progress.items() if m.get("phase") == "running"}),
        "borrow_idle": config["borrow_idle"],
        "retry_limit": 0 if config.get("frequency_version") else config.get("retry_concurrent", 1),
        "retry_running": sum(m.get("phase") == "running" and bool(m.get("failures")) for m in progress.values()),
        "oldest_wait_seconds": round(max(waits, default=0)),
        **result,
    }


def blocking_task(key, meta, progress, now=None):
    """Only identify the running parent module that actually blocks this task."""
    now = time.time() if now is None else now
    if (
        meta.get("phase") != "queued"
        or meta.get("blocked")
        or meta.get("retry_at", 0) > now
        or meta.get("database_pressure")
        or meta.get("browser_blocked")
    ):
        return None
    from .frequency_policy import category, CATEGORIES
    from .export_labels import MODULES

    for (sid, task_key), running in progress.items():
        kind = running.get("kind", task_key.split(":")[0])
        if (
            (sid, task_key) == key
            or sid != key[0]
            or not occupies_slot(running)
            or kind != meta.get("kind", key[1].split(":")[0])
        ):
            continue
        scope = running.get("scope", {})
        cat = category(kind, scope)
        name = CATEGORIES.get(kind, {}).get(cat, CATEGORIES["__shared__"].get(cat, cat))
        if cat == "base" and scope.get("trade_range") == "realtime":
            name = "今日实时数据"
        return {
            "kind": kind,
            "task_name": MODULES.get(kind, kind) + " · " + name,
            "scope": scope,
            "started_at": running.get("started_at"),
            "elapsed_seconds": max(0, int(now - running["started_at"])) if running.get("started_at") else None,
            "read_finished": bool(running.get("read_finished")),
            "browser_waking": bool(running.get("browser_waking")),
            "slot_granted": bool(running.get("slot_granted")),
        }
    return None


def waiting_reason(key, meta, progress, config):
    if meta.get("phase") != "queued":
        return ""
    if meta.get("browser_waking"):
        return "按轮转名额唤醒店铺浏览器"
    if meta.get("slot_granted"):
        return "已获采集名额，等待浏览器准备"
    if meta.get("blocked"):
        return "等待处理登录或权限异常"
    if meta.get("retry_at", 0) > time.time():
        return "失败重试退避中"
    if meta.get("database_pressure"):
        return "等待入库队列缓解"
    if meta.get("browser_blocked"):
        return meta.get("browser_wait_stage") or "等待店铺浏览器独占操作（登录或维护中）"
    if meta.get("shop_round_yielding"):
        return "本轮派发时间已到，等待当前读取完成后让出店铺名额"
    if meta.get("shop_round_wait"):
        return "等待本店下一轮，当前轮次不追加新任务"
    running = [(k, m) for k, m in progress.items() if occupies_slot(m)]
    occupied = {k[0] for k, _ in running} | set(config.get("_rotation_shops", meta.get("rotation_occupied_shops", ())))
    waking = {k[0] for k, m in progress.items() if m.get("browser_waking")}
    if key[0] not in occupied and len(occupied) >= config.get("max_active_shops", 2):
        suffix = "；其中 " + str(len(waking & occupied)) + " 家正在唤醒" if waking & occupied else ""
        return "等待店铺轮转名额（最多 " + str(config.get("max_active_shops", 2)) + " 家并行" + suffix + "）"
    blocker = blocking_task(key, meta, progress)
    if blocker:
        if blocker["slot_granted"]:
            return (
                "等待同店同模块已获名额的任务唤醒浏览器"
                if blocker["browser_waking"]
                else "等待同店同模块已获名额的任务开始读取"
            )
        elapsed = f" · 已运行 {blocker['elapsed_seconds']} 秒" if blocker["elapsed_seconds"] is not None else ""
        action = "入库完成" if blocker["read_finished"] else "当前批次完成"
        return f"等待同店同模块{action}：{blocker['task_name']}{elapsed}"
    from .frequency_policy import limit

    if sum(k[0] == key[0] for k, _ in running) >= limit(config, key[0]):
        return "已达到单店铺并发上限"
    if (
        not config.get("frequency_version")
        and meta.get("failures")
        and sum(bool(m.get("failures")) for _, m in running) >= config.get("retry_concurrent", 1)
    ):
        return "等待异常重试名额"
    if len(running) >= config["max_concurrent"]:
        return "采集并发已满"
    if config.get("frequency_version"):
        return "等待调度补位"
    if not config["borrow_idle"]:
        return "等待所属队列名额"
    return "等待调度，重点与普通任务按配额执行"
