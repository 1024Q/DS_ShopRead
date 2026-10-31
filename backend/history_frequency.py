"""Explicit, editable cadence defaults for historical analytics reports only."""

import copy
from .platforms import PLATFORMS
from .platform_support import supports

REPORT_RANGES = {
    "transactions": ("yesterday", "week", "month"),
    "goodsdata": ("yesterday", "week", "month"),
    "ad_products": ("yesterday", "week", "month"),
    "ad_reports": ("yesterday", "week", "month"),
}
GROUPS = {
    "yesterday": {"id": "history_yesterday", "name": "昨日报表", "seconds": 900, "time": "", "timeout": 240},
    # month means rolling 30 days, not a closed calendar month.
    "rolling": {"id": "history_rolling", "name": "历史滚动报表", "seconds": 3600, "time": "", "timeout": 240},
}


def historical(kind, scope):
    return scope.get("trade_range") in REPORT_RANGES.get(kind, ())


def _target(groups, span, updates):
    wanted = GROUPS["yesterday" if span == "yesterday" else "rolling"]
    # Reuse an unmodified recommendation, but never overwrite an existing group.
    existing = next(
        (
            g
            for g in groups
            if (g["id"] == wanted["id"] or g["id"].startswith(wanted["id"] + "_"))
            and all(g.get(k) == v for k, v in wanted.items() if k not in ("id", "name"))
        ),
        None,
    )
    if existing:
        return existing["id"]
    ids = {g["id"] for g in groups}
    names = {g["name"] for g in groups}
    identifier = wanted["id"]
    suffix = 2
    while identifier in ids:
        identifier = wanted["id"] + "_" + str(suffix)
        suffix += 1
    name = wanted["name"]
    suffix = 2
    while name in names:
        name = wanted["name"] + " " + str(suffix)
        suffix += 1
    value = {**wanted, "id": identifier, "name": name}
    groups.append(value)
    updates[identifier] = value
    return identifier


def recommend(config, include_explicit=False):
    """Return existing settings API patches; explicit user assignments win by default.

    Callers may opt into moving explicitly assigned historical reports as a
    separately authorized action. Paused or slower tasks are never accelerated.
    Today's tasks, order windows and existing frequency/concurrency values are
    never changed, including when a recommended group was customized/deleted.
    """
    groups = copy.deepcopy(config["frequency_groups"])
    updates = {}
    assignments = []
    preserved = []
    fallback = "custom" if any(g["id"] == "custom" for g in groups) else groups[0]["id"]
    for platform in PLATFORMS:
        explicit = config.get("task_modes", {}).get(platform, {})
        inherited = config.get("platform_modes", {}).get(platform, {})
        for kind, spans in REPORT_RANGES.items():
            if not supports(platform, kind):
                continue
            for span in spans:
                task = kind + "@" + span
                if task in explicit and not include_explicit:
                    preserved.append({"platform": platform, "kind": task, "target": explicit[task]})
                    continue
                previous = explicit.get(task, inherited.get(kind, fallback))
                old_group = next((g for g in groups if g["id"] == previous), None)
                seconds = GROUPS["yesterday" if span == "yesterday" else "rolling"]["seconds"]
                slower = old_group and (old_group.get("time") or old_group["seconds"] >= seconds)
                target = previous if previous == "off" or slower else _target(groups, span, updates)
                if explicit.get(task) == target:
                    continue
                assignments.append({"platform": platform, "kind": task, "tab": "task", "target": target})
    return {"group_updates": updates, "module_assignments": assignments, "preserved_explicit": preserved}


def migrate(config):
    if config.get("history_frequency_version") == 1:
        return config
    c = copy.deepcopy(config)
    plan = recommend(c)
    c["frequency_groups"].extend(plan["group_updates"].values())
    for assignment in plan["module_assignments"]:
        c.setdefault("task_modes", {}).setdefault(assignment["platform"], {})[assignment["kind"]] = assignment["target"]
    c["history_frequency_version"] = 1
    return c
