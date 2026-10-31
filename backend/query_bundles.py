"""One configurable query with durable, independently verified range reads."""

BUNDLES = {
    **{k + "@history_query": (name + "-历史综合查询", tuple(k + "@" + r for r in ("yesterday", "week", "month")))
       for k, name in (("ad_reports", "推广报表"), ("goodsdata", "商品报表"),
                       ("transactions", "交易数据"), ("ad_products", "商品推广"))},
    "orders@recent_query": ("订单查询-今昨订单查询", ("orders@today", "orders@yesterday")),
    "orders@open_query": ("订单查询-未完结订单查询", ("orders@unpaid", "orders@unshipped", "orders@unreceived")),
    "aftersales@service_query": ("售后工作台-商家处理综合查询", ("aftersales@base", "aftersales@merchant_pending")),
}


def catalog(entries):
    by_id = {e["id"]: e for e in entries}
    members = {m: key for key, (_, values) in BUNDLES.items() for m in values}
    result, seen = [], set()
    for entry in entries:
        key = members.get(entry["id"])
        if not key:
            result.append(entry)
            continue
        if key in seen:
            continue
        seen.add(key)
        name, children = BUNDLES[key]
        parts = [by_id[m] for m in children if m in by_id]
        platforms = set.intersection(*(set(e["platforms"]) for e in parts))
        result.append({**entry, "id": key, "label": name, "name": name,
                       "members": [p["id"] for p in parts], "platforms": sorted(platforms),
                       "ranges": [p["name"] for p in parts]})
    return result


def project(steps):
    """Collapse presentation only; each constituent retains its durable scope."""
    from .frequency_policy import task_id

    lookup = {member: key for key, (_, values) in BUNDLES.items() for member in values}
    groups = {}
    for step in steps:
        key = lookup.get(task_id(step["kind"], step.get("scope", {})), step["cache_key"])
        groups.setdefault(key, []).append(step)
    result = []
    ranks = {"running": 0, "queued": 1, "deferred": 2, "waiting": 3}
    for key, parts in groups.items():
        if key not in BUNDLES or len(parts) == 1:
            result.extend(parts)
            continue
        current = min(parts, key=lambda p: ranks.get(p["phase"], 4))
        result.append({**current, "name": BUNDLES[key][0], "query_id": key,
                       "range_steps": parts, "stage": current["name"] + " · " + current.get("stage", ""),
                       "frequency": " / ".join(dict.fromkeys(p["frequency"] for p in parts))})
    return result
