"""Keep per-node policy separate from fleet runtime counters."""


def cached_preview(database, node):
    """Read-only offline display; never confirms or writes a node configuration."""
    import copy
    import hashlib
    from .database import snapshots
    from ..frequency_policy import present
    from ..task_catalog import expand_legacy
    from ..task_layout import migrate

    response_scope = "response:" + hashlib.sha256(b"system/settings?").hexdigest()
    candidates = []
    for scope in ("collector_status", response_scope):
        for row in database.list(snapshots, node=node, shop="0", scope=scope):
            payload = row.get("payload", {})
            value = payload.get("value", {}) if scope == response_scope else payload
            config = value.get("collection")
            if isinstance(config, dict) and config.get("frequency_version") == 1 and config.get("frequency_groups"):
                candidates.append((row["updated"], config))
    if not candidates:
        return None
    updated, source = max(candidates, key=lambda item: item[0])
    config = copy.deepcopy(source)
    if not config.get("task_layout"):
        config = expand_legacy(config)
    return {
        "collection": present(migrate(config)),
        "configuration_source": "cached",
        "configuration_read_only": True,
        "configuration_updated_at": updated,
        "configuration_notice": "节点暂不可用，展示最后上报配置与任务分组预览；连接后读取实际配置才能保存。",
    }


def matches_patch(path, patch, actual):
    if not actual:
        return False
    if path == "system/concurrency":
        return bool(patch) and all(actual.get(k) == v for k, v in patch.items())
    if "module_assignments" in patch and "settings_patch" in patch:
        return matches_patch(path, {"module_assignments": patch["module_assignments"]}, actual) and matches_patch(
            path, {"settings_patch": patch["settings_patch"]}, actual
        )
    if "module_assignments" in patch:
        fields = {
            "task": "task_modes",
            "step": "task_modes",
            "queue": "platform_queues",
            "frequency": "platform_modes",
            "priority": "platform_priorities",
        }
        return all(
            actual.get(fields.get(a["tab"], ""), {}).get(a["platform"], {}).get(a["kind"])
            == (None if a["target"] == "default" else a["target"])
            for a in patch["module_assignments"]
        )
    if "settings_patch" in patch and "task_layout_patch" in patch["settings_patch"]:
        from ..task_layout import matches_patch as layout_matches

        expected = dict(patch["settings_patch"])
        layout = expected.pop("task_layout_patch")
        return layout_matches(layout, actual.get("task_layout")) and (
            not expected or matches_patch(path, {"settings_patch": expected}, actual)
        )
    if "settings_patch" in patch and "group_updates" in patch["settings_patch"]:
        groups = {g["id"]: g for g in actual.get("frequency_groups", [])}
        p = patch["settings_patch"]
        return all(
            groups.get(k) is None if v is None else all(groups.get(k, {}).get(field) == val for field, val in v.items())
            for k, v in p["group_updates"].items()
        ) and all(actual.get(k) == v for k, v in p.items() if k not in ("group_updates", "group_moves"))
    if "settings_patch" in patch:
        return all(
            all(actual.get(k, {}).get(n) == v for n, v in val.items())
            if isinstance(val, dict)
            else actual.get(k) == val
            for k, val in patch["settings_patch"].items()
        )
    for key, field, target in [
        ("queue_assignment", "platform_queues", "queue"),
        ("frequency_assignment", "platform_modes", "mode"),
    ]:
        if key in patch:
            a = patch[key]
            return actual.get(field, {}).get(a["platform"], {}).get(a["kind"]) == (
                None if a[target] == "default" else a[target]
            )
    return all(actual.get(k) == v for k, v in patch.items())


def aggregate_status(merged, values):
    configs = [v.get("collection", {}) for v in values]
    shared = {
        k: configs[0].get(k) if all(c.get(k) == configs[0].get(k) for c in configs) else None
        for k in set().union(*(c.keys() for c in configs))
    }
    if shared.get("frequency_groups") is None:
        groups = {}
        for c in configs:
            for g in c.get("frequency_groups", []):
                groups.setdefault(g["id"], g)
        shared["frequency_groups"] = list(groups.values())
    queues = [v.get("queues", {}) for v in values]
    result = {
        **merged,
        "collection": shared,
        "scope": "all",
        "machine": None,
        "configured_max_concurrent": sum(c.get("max_concurrent", 0) or 0 for c in configs),
    }
    result["summary"] = {
        k: sum(v.get("summary", {}).get(k, 0) for v in values)
        for k in ("running", "queued", "errors", "waiting_login", "cached", "deferred")
    }
    if "diagnostics" in merged:
        result["diagnostics"] = {
            **merged["diagnostics"],
            **{
                k: sum(v.get("diagnostics", {}).get(k, 0) for v in values)
                for k in (
                    "completed_15m",
                    "success_15m",
                    "failed_15m",
                    "tasks_per_minute",
                    "merged_demands",
                    "deferred_count",
                    "slow_sources",
                    "archived_tasks",
                    "task_count",
                )
            },
        }
    if queues:
        result["queues"] = {
            **merged.get("queues", {}),
            **{k: sum(q.get(k, 0) for q in queues) for k in ("max_concurrent", "retry_limit", "retry_running")},
        }
        for lane in {k for q in queues for k, v in q.items() if isinstance(v, dict) and "running" in v}:
            result["queues"][lane] = {
                k: sum(q.get(lane, {}).get(k, 0) for q in queues) for k in ("running", "queued", "reserved", "borrowed")
            }
    dispatches = [v['dispatch'] for v in values if v.get('dispatch', {}).get('limits')]
    if dispatches:
        from collections import Counter
        reasons = Counter()
        for d in dispatches:reasons.update(d.get('waiting_reasons', {}))
        result['dispatch'] = {
            **{k:sum(d.get(k,0) for d in dispatches) for k in
               ('reading','database','resuming','finishing','executions','preparing_shops','plans')},
            'waiting_reasons':dict(reasons),
            'reason_labels':{k:v for d in dispatches for k,v in d.get('reason_labels',{}).items()},
            'limits':{'max_concurrent':sum(d['limits'].get('max_concurrent',0) for d in dispatches)},
            'reported_nodes':len(dispatches),
        }
    return result
