"""Editable parent tasks, independent of leaf cadence and execution identities."""

import copy
import re

VERSION = 1


def defaults():
    from .frequency_policy import step_catalog
    from .task_catalog import GROUPS, HOME
    from .platforms import PLATFORMS

    metrics = {"overview@base", "transactions@base", "ad_reports@base"}
    definitions = [
        ("home_metrics", "首页经营指标", metrics),
        ("home_todos", "首页预警与待办", HOME - metrics - {"products@base"}),
        ("product_catalog", "商品目录与上新", ("products@base",)),
        ("product_live", "商品实时经营数据", GROUPS["goodsdata@live"][1]),
        ("orders_recent", "订单查询-今昨订单", GROUPS["orders@recent"][1]),
        ("orders_unfinished", "订单查询-未完结订单回查", GROUPS["orders@unfinished"][1]),
        ("orders_recheck", "订单查询-精准回查", ("orders@recheck",)),
        ("aftersales_work", "售后与工单待办", GROUPS["aftersales@work"][1]),
        ("daily_business", "日常业务综合采集", GROUPS["tax@routine"][1]),
        ("product_diagnostics", "商品诊断与权益", GROUPS["navigator@diagnostics"][1]),
        ("history_reports", "历史报表与对账", GROUPS["transactions@reports"][1]),
        ("invoice_details", "订单开票-明细补采", ("invoices@details",)),
    ]
    groups = [{"id": key, "name": name} for key, name, _ in definitions]
    parents = {step: key for key, _, members in definitions for step in members}
    assignments = {p: {} for p in PLATFORMS}
    for step in step_catalog():
        key = parents.get(step["id"])
        if not key:
            key = "task_" + step["id"].replace("@", "_")
            groups.append({"id": key, "name": step["label"]})
        for platform in step["platforms"]:
            assignments[platform][step["id"]] = key
    return {"version": VERSION, "groups": groups, "assignments": assignments}


def migrate(config):
    """Snapshot old effective leaf modes once; never change frequency values."""
    if config.get("task_layout"):
        return config
    from .task_catalog import GROUPS

    result = copy.deepcopy(config)
    for assignments in result.get("task_modes", {}).values():
        for parent, (_, members) in GROUPS.items():
            if parent in assignments:
                for step in members:
                    assignments[step] = assignments[parent]
    result["task_layout"] = defaults()
    return result


def resolve(config, platform, step_id):
    layout = config.get("task_layout") or defaults()
    parent = layout.get("assignments", {}).get(platform, {}).get(step_id)
    found = next((g for g in layout.get("groups", []) if g["id"] == parent), None)
    if found:
        return {"id": found["id"], "name": found["name"]}
    # A manual/unsupported scope may still have runtime progress; keep its stable
    # identity visible without creating a scheduled task or mutating settings.
    from .frequency_policy import step_catalog

    step = next((s for s in step_catalog() if s["id"] == step_id), None)
    return {"id": step_id, "name": step["label"] if step else step_id}


def apply_patch(config, patch):
    """Apply only edited groups/assignments; validate the final layout atomically."""
    from .frequency_policy import step_catalog

    if not isinstance(patch, dict) or set(patch) - {"group_updates", "step_assignments", "group_moves"}:
        raise ValueError("任务分组修改项无效")
    layout = copy.deepcopy(config.get("task_layout") or defaults())
    if layout.get("version") != VERSION:
        raise ValueError("任务分组版本已变化，请刷新后重试")
    groups = {g["id"]: g for g in layout["groups"]}
    previous = set(groups)
    updates = patch.get("group_updates", {})
    if not isinstance(updates, dict):
        raise ValueError("任务修改项无效")
    for identifier, fields in updates.items():
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", identifier):
            raise ValueError("任务编号无效")
        if fields is None:
            groups.pop(identifier, None)
        elif isinstance(fields, dict) and set(fields) <= {"name"}:
            groups[identifier] = {**groups.get(identifier, {}), **fields, "id": identifier}
        else:
            raise ValueError("任务只能修改名称；更新频率请在子步骤中设置")
    if not groups:
        raise ValueError("至少保留一个后台任务")
    names = []
    for group in groups.values():
        name = group.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("任务名称应为 1 至 80 个字符")
        group["name"] = name.strip()
        names.append(group["name"])
    if len(set(names)) != len(names):
        raise ValueError("任务名称不能重复")
    steps = {s["id"]: s for s in step_catalog()}
    changes = patch.get("step_assignments", [])
    if not isinstance(changes, list):
        raise ValueError("子步骤分配列表无效")
    seen = set()
    for item in changes:
        if not isinstance(item, dict) or set(item) != {"platform", "step_id", "group_id"}:
            raise ValueError("子步骤分配项无效")
        platform, step_id, target = item["platform"], item["step_id"], item["group_id"]
        if not all(isinstance(v, str) for v in (platform, step_id, target)):
            raise ValueError("子步骤分配项无效")
        if step_id not in steps or platform not in steps[step_id]["platforms"]:
            raise ValueError("该平台未接入此子步骤，请刷新配置")
        if target not in groups:
            raise ValueError("目标任务不存在，请刷新配置")
        if (platform, step_id) in seen:
            raise ValueError("同一子步骤只能分配一次")
        seen.add((platform, step_id))
        layout["assignments"].setdefault(platform, {})[step_id] = target
    removed = previous - set(groups)
    moves = patch.get("group_moves", {})
    if not isinstance(moves, dict) or any(
        source not in updates or updates[source] is not None or not isinstance(target, str) or target not in groups
        for source, target in moves.items()
    ):
        raise ValueError("删除任务必须选择有效迁入任务")
    for assignments in layout["assignments"].values():
        for step_id, target in list(assignments.items()):
            if target in removed:
                if target not in moves:
                    raise ValueError("任务仍有子步骤，请先转移或选择迁入任务")
                assignments[step_id] = moves[target]
    for step in steps.values():
        if any(layout["assignments"].get(p, {}).get(step["id"]) not in groups for p in step["platforms"]):
            raise ValueError("每个子步骤必须属于一个有效任务")
    layout["groups"] = list(groups.values())
    return layout


def matches_patch(patch, actual):
    """Verify only requested edits on each node, not a copied fleet snapshot."""
    if not isinstance(actual, dict) or actual.get("version") != VERSION:
        return False
    groups = {g["id"]: g for g in actual.get("groups", [])}
    for identifier, fields in patch.get("group_updates", {}).items():
        if fields is None:
            if identifier in groups:
                return False
        elif any(groups.get(identifier, {}).get(k) != v.strip() for k, v in fields.items()):
            return False
    for item in patch.get("step_assignments", []):
        if actual.get("assignments", {}).get(item["platform"], {}).get(item["step_id"]) != item["group_id"]:
            return False
    removed = {key for key, value in patch.get("group_updates", {}).items() if value is None}
    return not any(target in removed for values in actual.get("assignments", {}).values() for target in values.values())
