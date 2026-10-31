"""Bound waiting jobs, keeping due demand visible and ordered fairly outside workers."""

from collections import Counter
from . import collection

# Single source for the fallback; the scheduler always passes the saved value.
DEFAULT_SHOP_CONCURRENT = collection.DEFAULTS["shop_concurrent"]


async def make_core_room(service, candidates, config):
    # No module or core-data priority preemption.
    return


def choose(candidates, progress, config, now):
    from .collection_queue import queue_caps

    caps = queue_caps(config)
    from .collection_queue import queue_for

    def group(kind, options, platform):
        return queue_for({"kind": kind, "scope": options, "platform": platform}, config)

    used = Counter()
    shops = Counter()
    queued_modules = set()
    for (sid, _), m in progress.items():
        if m.get("phase") == "queued":
            used[group(m["kind"], m.get("scope", {}), m.get("platform", "pdd"))] += 1
            shops[(sid, group(m["kind"], m.get("scope", {}), m.get("platform", "pdd")))] += 1
            queued_modules.add((sid, m["kind"]))

    def order(c):
        shop, item, kind, options, state = c
        from .demand_refresh import order
        from .refund_priority import demand

        return (not demand(kind, options, state, shop.get("platform", "pdd")), *order(kind, options, state, config, shop.get("platform", "pdd"), now), shop["id"], kind)

    if config.get("frequency_version"):
        from .master_tasks import representatives

        selected, deferred = representatives(candidates, progress, config, now)
        return sorted(selected, key=order), deferred, caps
    selected = []
    deferred = []
    idle_used = any(m.get("scope", {}).get("_order_lane") in ("history", "daily") for m in progress.values())
    for c in sorted(candidates, key=order):
        shop, item, kind, options, state = c
        g = group(kind, options, shop.get("platform", "pdd"))
        if (shop["id"], kind) in queued_modules:
            deferred.append(c)
            continue
        idle = options.get("_order_lane") in ("history", "daily")
        if idle and idle_used:
            deferred.append(c)
            continue
        if used[g] >= caps.get(g, 0) or shops[(shop["id"], g)] >= config.get(
            "shop_concurrent", DEFAULT_SHOP_CONCURRENT
        ):
            deferred.append(c)
            continue
        selected.append(c)
        queued_modules.add((shop["id"], kind))
        idle_used |= idle
        used[g] += 1
        shops[(shop["id"], g)] += 1
    return selected, deferred, caps
