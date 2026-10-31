"""Stable per-shop master tasks backed by the existing durable step ledgers.

No replacement checkpoint or publication format: each step remains independently
recoverable and a code rollback can continue the same saved work.
"""

import time
from .task_catalog import title
from .frequency_policy import task_id
from .task_layout import resolve


def identifier(kind, scope, config=None, platform="pdd"):
    return resolve(config or {}, platform, task_id(kind, scope))["id"]


def key(sid, kind, scope, config=None, platform="pdd"):
    return sid, identifier(kind, scope, config, platform)


def label(kind, scope, config, platform="pdd"):
    parent = resolve(config, platform, task_id(kind, scope))
    name = title(kind, scope).split(" / ", 1)[-1]
    return parent["name"] + " / " + name


def enabled(config):
    return bool(config.get("frequency_version"))


def representatives(candidates, progress, config, now):
    """One ready demand per shop/business; masters only group presentation.

    Independent businesses in one master may use the configured shop capacity.
    Keep alternative scopes of the same business outside the worker queue until
    its current batch finishes, preserving their own durable attempts/checkpoints.
    """
    active = {
        (k[0], m.get("kind", k[1].split(":")[0]))
        for k, m in progress.items()
        if m.get("phase") in ("queued", "running")
    }
    running = {(k[0], m.get("kind", k[1].split(":")[0])) for k, m in progress.items() if m.get("phase") == "running" or m.get("slot_granted")}
    from .demand_refresh import order

    groups = {}
    deferred = []
    for c in candidates:
        shop, item, kind, scope, state = c
        business = (shop["id"], kind)
        from .refund_priority import demand

        if business in running or business in active and not demand(kind, scope, state, shop.get("platform", "pdd")):
            deferred.append(c)
        else:
            groups.setdefault(business, []).append(c)
    chosen = []
    for steps in groups.values():

        def rank(c):
            s, _, k, o, state = c
            from .refund_priority import demand

            return (not demand(k, o, state, s.get("platform", "pdd")), state.get("last_attempt", 0), *order(k, o, state, config, s.get("platform", "pdd"), now), k)

        steps.sort(key=rank)
        chosen.append(steps[0])
        deferred.extend(steps[1:])
    return chosen, deferred


def project(service):
    """One UI record per master; completed/waiting steps never inflate its queue."""
    config = getattr(service, "queue_config", {})
    if not enabled(config):
        return []
    known = dict(getattr(service, "master_steps", {}))
    from .collection_metrics import recent, enrich
    history = recent(service.store.directory) if getattr(getattr(service, 'store', None), 'directory', None) else {}
    for k, m in list(getattr(service, "deferred", {}).items()):
        known[k] = {**known.get(k, {}), **m, "phase": "deferred"}
    for k, m in list(service.progress.items()):
        known[k] = {**known.get(k, {}), **m}
    groups = {}
    from .collection import frequency_name
    from .collection_queue import waiting_reason, scheduling_config

    wait_config = scheduling_config(service)
    from .queue_eta import estimates

    try:
        eta = estimates(service)
    except Exception:
        eta = {}
    for (sid, cache_key), meta in known.items():
        kind = meta.get("kind", cache_key.split(":")[0])
        scope = meta.get("scope", {})
        from .shop_rotation import home_paused

        if home_paused(kind, scope, config) and meta.get("phase") != "running":
            continue
        platform = meta.get("platform", "pdd")
        step_id = task_id(kind, scope)
        parent = resolve(config, platform, step_id)
        master = parent["id"]
        row = groups.setdefault(
            (sid, master), {"shop_id": sid, "master_id": master, "module": parent["name"], "steps": []}
        )
        phase = meta.get("phase", "waiting")
        stage = (meta.get('resource_wait') if meta.get('execution_phase') == 'resuming' else None) or meta.get("stage", "等待下次执行")
        if phase == "deferred" and stage == "等待下次执行":
            phase = "waiting"
        if phase == "queued":
            stage = waiting_reason((sid, cache_key), meta, service.progress, wait_config)
        row["steps"].append(
            {
                **meta,
                **enrich(service, sid, cache_key, meta, history),
                "kind": kind,
                "scope": scope,
                "cache_key": cache_key,
                "step_id": step_id,
                "master_id": master,
                "name": title(kind, scope).split(" / ", 1)[-1],
                "phase": phase,
                "stage": stage,
                "frequency": frequency_name(kind, scope, config, platform),
                **(eta.get((sid, cache_key), {}) if phase == "queued" else {}),
            }
        )
    ranks = {"running": 0, "queued": 1, "deferred": 2, "waiting": 3}
    result = []
    for row in groups.values():
        from .query_bundles import project as query_steps

        range_steps = row["steps"]
        row["steps"] = query_steps(row["steps"])
        row["steps"].sort(key=lambda m: (ranks.get(m["phase"], 4), m.get("last_attempt", 0), m["name"]))
        current = row["steps"][0]
        running = [s for s in row["steps"] if s["phase"] == "running"]
        queued = [s for s in range_steps if s.get("queue_position")]
        first = min(queued, key=lambda s: s["queue_position"], default=None)
        if first:
            row.update(
                queue_position=first["queue_position"],
                queue_total=first.get("queue_total"),
                wait_seconds=first.get("wait_seconds"),
            )
        row.update(
            timing_seconds={phase: round(sum((s.get('timing_seconds') or (s.get('last_timing') or {}).get('timing_seconds', {})).get(phase, 0) for s in range_steps), 3)
                            for phase in ('slot_wait', 'browser_start', 'page_load', 'query', 'database')},
            timing_basis='各子步骤最近执行的累计耗时（包含并行）',
            phase=current["phase"],
            current_step="、".join(s["name"] for s in running) if running else current["name"],
            stage="同时执行 " + str(len(running)) + " 个子步骤" if len(running) > 1 else current["stage"],
            running_steps=sum(s["phase"] == "running" for s in range_steps),
            queued_steps=sum(s["phase"] == "queued" for s in range_steps),
            step_count=len(range_steps),
            query_count=len(row["steps"]),
            last_success=max((m.get("last_success", 0) for m in row["steps"]), default=0),
        )
        result.append(row)
    # Parent and child positions describe different queues. Never reuse a
    # child's position as the parent's queue number.
    parents = sorted(
        (r for r in result if r.get("queue_position")),
        key=lambda r: (r["queue_position"], r["shop_id"], r["master_id"]),
    )
    for position, row in enumerate(parents, 1):
        from .queue_eta import namespace

        row["first_step_queue_position"] = row["queue_position"]
        row.update(queue_position=position, queue_total=len(parents), queue_number=f"{namespace(service)}/M{position}")
    return sorted(result, key=lambda r: (r["shop_id"], r["master_id"]))


def counts(rows):
    return {p: sum(r["phase"] == p for r in rows) for p in ("running", "queued", "deferred", "waiting")}


def remember(service, sid, cache_key, kind, scope, state):
    if not hasattr(service, "master_steps"):
        service.master_steps = {}
    fields = (
        "last_success",
        "last_attempt",
        "last_requested",
        "failures",
        "retry_at",
        "last_error",
        "continuation",
        "blocked",
        "platform",
    )
    service.master_steps[sid, cache_key] = {
        **{k: state[k] for k in fields if k in state},
        "kind": kind,
        "scope": dict(scope),
        "phase": "deferred" if state.get("failures") or state.get("continuation") else "waiting",
        "stage": "重试冷却"
        if state.get("failures")
        else "断点已保存，等待下一批"
        if state.get("continuation")
        else "等待下次执行",
    }


def listing(service, group, page):
    from .shop_identity import shop_label
    from datetime import datetime
    from .business_reader import BEIJING
    from .read_budget import remaining

    rows = project(service)
    shops = {s["id"]: s for s in service.store.list()}
    chosen = [r for r in rows if r["phase"] == group or group == "deferred" and r["phase"] == "waiting"]
    categories = {}
    out = []
    now = time.time()
    stamp = lambda n: datetime.fromtimestamp(n, BEIJING).strftime("%m-%d %H:%M:%S") if n else "—"
    for row in chosen:
        for step in row["steps"]:
            started = step.get("started_at")
            step.update(
                started=stamp(started),
                elapsed=f"{max(0, int(now - started))} 秒" if started else "—",
                remaining=(
                    "入库中 · 独立等待"
                    if step.get("read_finished")
                    else "数据库等待 · 读取计时暂停"
                    if step.get("timeout_paused")
                    else str(remaining(step, now)) + " 秒"
                )
                if started
                else "未开始计时",
            )
        m = row["steps"][0]
        stage = row["stage"]
        categories[stage] = categories.get(stage, 0) + 1
        running = [s for s in row["steps"] if s["phase"] == "running"]
        first = min((s["started_at"] for s in running if s.get("started_at")), default=m.get("started_at"))

        def step_detail(step):
            detail = step["name"] + "：" + step["stage"]
            if step["phase"] in ("deferred", "waiting"):
                if step.get("retry_at", 0) > now:
                    detail += f" · {max(0, int(step['retry_at'] - now))} 秒后重试"
                elif step.get("next_run_at", 0) > now:
                    detail += f" · {max(0, int(step['next_run_at'] - now))} 秒后到期"
                if step.get("reason"):
                    detail += " · " + step["reason"]
            return detail

        detail = "；".join(step_detail(step) for step in row["steps"])
        if row.get("queue_position"):
            from .queue_eta import describe

            detail = (
                f"队列第 {row['queue_position']}/{row.get('queue_total') or '—'} 位 · 预计 "
                + describe(row.get("wait_seconds"))
                + "后开始；"
                + detail
            )
        out.append(
            {
                **row,
                "shop": shop_label(shops.get(row["shop_id"], {"id": row["shop_id"]})),
                "frequency": " / ".join(dict.fromkeys(s["frequency"] for s in row["steps"])),
                "detail": f"当前步骤：{row['current_step']} · {stage}；运行 {row['running_steps']} 个、排队 {row['queued_steps']} 个，共 {row['step_count']} 个子步骤。{detail}",
                "scope": " · ".join(str(m.get("scope", {}).get(k) or "") for k in ("date_from", "date_to", "status")),
                "generated": stamp(min((s.get("queued_at") or s.get("last_requested") or now for s in row["steps"]))),
                "started": stamp(first),
                "elapsed": f"{max(0, int(now - first))} 秒" if first else "—",
                "remaining": "；".join(s["name"] + "：" + s["remaining"] for s in running)
                if len(running) > 1
                else m["remaining"],
                "next_run": stamp(m.get("next_run_at")),
            }
        )
    pages = max(1, (len(out) + 29) // 30)
    page = min(page, pages)
    return {
        "rows": out[(page - 1) * 30 : page * 30],
        "total": len(out),
        "page": page,
        "page_count": pages,
        "categories": categories if group == "deferred" else {},
    }
