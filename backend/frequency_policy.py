"""Extensible cadence groups and task categories; parent module owns exclusion."""

import copy, json, re
from datetime import datetime, timedelta
from .business_reader import BEIJING

CATEGORIES = {
    "orders": {
        "today": "今日订单",
        "yesterday": "昨日订单",
        "unpaid": "待付款回查",
        "unshipped": "待发货回查",
        "unreceived": "待收货回查",
        "shipping_due": "即将逾期发货",
        "refund": "退款成功订单",
        "recheck": "订单精准回查",
        "history": "历史补采",
        "daily": "每日核对",
    },
    "aftersales": {"merchant_pending": "待商家处理", "expiring": "待处理即将逾期"},
    "tickets": {"pending": "待处理工单"},
    "__shared__": {
        "week": "近7天数据",
        "base": "常规任务",
        "attention": "重点状态",
        "yesterday": "昨日数据",
        "month": "近30天数据",
        "quarter": "近90天数据",
        "details": "明细补采",
    },
}


def category(kind, scope):
    if kind == "orders":
        lane = scope.get("_order_lane")
        status = scope.get("status", "")
        if lane == "recent":
            return "today" if scope.get("date_from") == datetime.now(BEIJING).date().isoformat() else "yesterday"
        if lane in ("history", "daily", "recheck"):
            return lane
        found = {
            "待付款": "unpaid",
            "待发货": "unshipped",
            "待收货": "unreceived",
            "即将逾期发货": "shipping_due",
            "退款成功": "refund",
        }.get(status)
        if found:
            return found
    if scope.get("_attention"):
        return "attention"
    if kind == "aftersales" and scope.get("status") in ("待商家处理", "待处理即将逾期"):
        return "merchant_pending" if scope["status"] == "待商家处理" else "expiring"
    if kind == "tickets" and scope.get("status") == "待处理":
        return "pending"
    if scope.get("_invoice_details_refresh"):
        return "details"
    return scope.get("trade_range") if scope.get("trade_range") in ("yesterday", "week", "month", "quarter") else "base"


def task_id(kind, scope):
    return kind + "@" + category(kind, scope)


def step_catalog():
    """Stable leaf identities; organization and cadence are separate settings."""
    from .business import KINDS
    from .attention import supported
    from .export_labels import MODULES
    from .platforms import PLATFORMS
    from .platform_support import supports
    from .task_catalog import HOME, REMOVED

    result = []
    for k in sorted(KINDS):
        choices = {"base": "常规任务"}
        if k in CATEGORIES:
            choices.update(CATEGORIES[k])
        if supported("pdd", k):
            choices["attention"] = "重点状态"
        if k in ("transactions", "goodsdata", "ad_products", "ad_reports"):
            choices.update({x: CATEGORIES["__shared__"][x] for x in ("yesterday", "week", "month", "quarter")})
        if k == "invoices":
            choices["details"] = "明细补采"
        for v, name in choices.items():
            identifier = k + "@" + v
            if identifier in REMOVED:
                continue
            result.append(
                {
                    "id": identifier,
                    "kind": k,
                    "name": name,
                    "label": MODULES.get(k, k) + "-" + name,
                    "home": identifier in HOME,
                    "platforms": [p for p in PLATFORMS if supports(p, k) and (v != "attention" or supported(p, k))],
                }
            )
    return result


def catalog():
    from .task_catalog import decorate

    return decorate(step_catalog())


def present(config):
    from .query_bundles import catalog as queries

    return {**config, "task_catalog": catalog(), "task_step_catalog": queries(step_catalog())}


def migrate(config):
    c = copy.deepcopy(config)
    c.setdefault("home_paused", False)
    c.setdefault("max_active_shops", 2)
    c.setdefault("browser_idle_seconds", 30)
    if c.get("frequency_version") != 1:
        ids = ["realtime", "custom", "daily", "custom2", "custom3", "custom4"]
        c["frequency_groups"] = [
            {
                "id": i,
                "name": c.get("frequency_names", {}).get(i, "自定义频率 3" if i == "custom4" else i),
                "seconds": 86400 if i == "daily" else c.get(i + "_seconds", 1800),
                "time": c.get("daily_time", "03:00") if i == "daily" else "",
                "timeout": 240,
            }
            for i in ids
        ]
        # Three previously visible groups remain; three new groups start empty.
        for i, g in enumerate(c["frequency_groups"][3:], 1):
            g["name"] = "自定义频率 " + str(i)
        c.update(frequency_version=1, task_modes={}, shop_limits={})
        c.setdefault("max_concurrent", 20)
        c.setdefault("shop_concurrent", 3)
    c.update(adaptive_enabled=False, change_adaptive_enabled=False, stagger_seconds=0, borrow_idle=True)
    from .history_frequency import migrate as migrate_history
    from .task_catalog import expand_legacy

    from .task_layout import migrate as migrate_layout

    c = migrate_history(c)
    if not c.get("task_layout"):
        c = expand_legacy(c)
    return migrate_layout(c)


def selected(kind, scope, c, platform):
    from .list_completion import frequency as completion_frequency

    once = completion_frequency(kind, scope, c)
    if once:
        return once
    from .task_catalog import removed_report

    if removed_report(kind, scope):
        return "off"
    fallback = "custom" if any(g["id"] == "custom" for g in c["frequency_groups"]) else c["frequency_groups"][0]["id"]
    from .task_catalog import grouped_id

    identifier = task_id(kind, scope)
    assignments = c.get("task_modes", {}).get(platform, {})
    from .home_only_pause import MARKER, REVISION
    from .task_catalog import HOME

    if c.get(MARKER) == REVISION and identifier not in HOME:
        fallback = "off"
    if c.get("task_layout", {}).get("version") == 1:
        return assignments.get(identifier, c.get("platform_modes", {}).get(platform, {}).get(kind, fallback))
    return assignments.get(
        grouped_id(identifier),
        assignments.get(identifier, c.get("platform_modes", {}).get(platform, {}).get(kind, fallback)),
    )


def group(kind, scope, c, platform):
    if not c.get("frequency_version"):
        return None
    selected_id = selected(kind, scope, c, platform)
    return next((g for g in c.get("frequency_groups", []) if g["id"] == selected_id), None)


def limit(c, sid):
    return c.get("shop_limits", {}).get(str(sid), c.get("shop_concurrent", 3))


def save(store, value, current):
    from .platforms import PLATFORMS

    c = copy.deepcopy(current)
    c.pop("task_catalog", None)
    c.pop("task_step_catalog", None)
    if "module_assignments" in value:
        if set(value) - {"module_assignments", "settings_patch"}:
            raise ValueError("模块修改项无效")
        valid = {x["id"] for x in catalog()}
        steps = {x["id"]: x for x in step_catalog()}
        groups = {g["id"] for g in c["frequency_groups"]} | {"off"}
        if not isinstance(value["module_assignments"], list) or not value["module_assignments"]:
            raise ValueError("模块修改列表无效")
        seen = set()
        for a in value["module_assignments"]:
            if (
                not isinstance(a, dict)
                or set(a) != {"platform", "kind", "tab", "target"}
                or any(not isinstance(v, str) for v in a.values())
            ):
                raise ValueError("模块修改项无效")
            identity = (a["platform"], a["kind"], a["tab"])
            if identity in seen:
                raise ValueError("同一任务或子步骤只能修改一次")
            seen.add(identity)
            if (
                a.get("platform") not in PLATFORMS
                or a.get("tab") not in ("task", "step")
                or a.get("kind") not in (valid if a.get("tab") == "task" else steps)
                or a.get("target") not in groups
            ):
                raise ValueError("任务分类或频率组不存在，请刷新配置")
            if a["tab"] == "step":
                if a["platform"] not in steps[a["kind"]]["platforms"]:
                    raise ValueError("该平台未接入此子步骤")
                c.setdefault("task_modes", {}).setdefault(a["platform"], {})[a["kind"]] = a["target"]
            else:
                from .task_catalog import assign

                assign(c, a["platform"], a["kind"], a["target"])
    if "settings_patch" in value or "module_assignments" not in value:
        patch = value.get("settings_patch", value)
        allowed = {
            "frequency_groups",
            "group_updates",
            "group_moves",
            "enabled",
            "keepalive_enabled",
            "keepalive_seconds",
            "batch_pages",
            "batch_seconds",
            "max_concurrent",
            "shop_concurrent",
            "shop_limits",
            "home_paused",
            "max_active_shops",
            "browser_idle_seconds",
            "task_layout_patch",
        }
        if not isinstance(patch, dict) or set(patch) - allowed:
            raise ValueError("配置项已调整，请刷新页面后重试")
        patch = dict(patch)
        if "task_layout_patch" in patch:
            from .task_layout import apply_patch

            c["task_layout"] = apply_patch(c, patch.pop("task_layout_patch"))
        if "group_updates" in patch:
            updates = patch.pop("group_updates")
            if not isinstance(updates, dict):
                raise ValueError("频率修改项无效")
            groups = {g["id"]: g for g in current["frequency_groups"]}
            for key, value in updates.items():
                if value is None:
                    groups.pop(key, None)
                elif isinstance(value, dict) and value.get("id", key) == key:
                    groups[key] = {**groups.get(key, {}), **value, "id": key}
                else:
                    raise ValueError("频率编号不可修改")
            patch["frequency_groups"] = list(groups.values())
        moves = patch.get("group_moves", {})
        c.update({k: v for k, v in patch.items() if k != "group_moves"})
        if "frequency_groups" in patch:
            if not isinstance(c["frequency_groups"], list) or any(
                not isinstance(g, dict) or not isinstance(g.get("id"), str) for g in c["frequency_groups"]
            ):
                raise ValueError("频率组格式无效")
            removed = {g["id"] for g in current["frequency_groups"]} - {g["id"] for g in c["frequency_groups"]}
            ids = {g["id"] for g in c["frequency_groups"]}
            moves = dict(moves)
            for source in removed:
                target = moves.get(source)
                seen = {source}
                while target in removed and target not in seen:
                    seen.add(target)
                    target = moves.get(target)
                moves[source] = target
            if any(moves.get(k) not in ids for k in removed):
                raise ValueError("删除频率组必须选择有效迁入组")
            for field in ("platform_modes", "task_modes"):
                for assignments in c.get(field, {}).values():
                    for k, v in list(assignments.items()):
                        if v in removed:
                            assignments[k] = moves[v]
    groups = c["frequency_groups"]
    if not isinstance(groups, list) or not groups:
        raise ValueError("至少保留一个采集频率组")
    if any(not isinstance(g.get("name"), str) for g in groups):
        raise ValueError("频率名称无效")
    if len({g["id"] for g in groups}) != len(groups) or len({g["name"].strip() for g in groups}) != len(groups):
        raise ValueError("频率编号和名称不能重复")
    for g in groups:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", g["id"]) or not isinstance(g["name"], str) or not g["name"].strip():
            raise ValueError("频率名称或编号无效")
        if any(type(g.get(k)) is not int or g[k] < 1 for k in ("seconds", "timeout")):
            raise ValueError("间隔和超时应为正整数秒")
        if g.get("time"):
            try:
                datetime.strptime(g["time"], "%H:%M")
            except ValueError:
                raise ValueError("每日执行时间无效") from None
    for k in ("max_concurrent", "shop_concurrent", "max_active_shops"):
        if type(c[k]) is not int or c[k] < 1:
            raise ValueError("并发数量应为正整数")
    if not isinstance(c["shop_limits"], dict) or any(
        not str(k).isdigit() or type(v) is not int or v < 1 for k, v in c["shop_limits"].items()
    ):
        raise ValueError("单店并发配置无效")
    if type(c["browser_idle_seconds"]) is not int or c["browser_idle_seconds"] < 0:
        raise ValueError("浏览器空闲时间应为非负整数秒，0 表示不自动关闭")
    for key in ("enabled", "keepalive_enabled", "home_paused"):
        if type(c[key]) is not bool:
            raise ValueError("开关配置无效")
    for key in ("keepalive_seconds", "batch_pages", "batch_seconds"):
        if type(c[key]) is not int or c[key] < 1:
            raise ValueError("执行参数应为正整数")
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('collection_policy_v3',?)", (json.dumps(c),))
    return present(c)


def next_run(kind, scope, c, platform, last_success, now):
    g = group(kind, scope, c, platform)
    if not g:
        return None
    if g.get("time"):
        current = datetime.fromtimestamp(now, BEIJING)
        h, m = map(int, g["time"].split(":"))
        boundary = current.replace(hour=h, minute=m, second=0, microsecond=0)
        if boundary.timestamp() <= last_success:
            boundary += timedelta(days=1)
        return boundary.timestamp()
    return last_success + g["seconds"]
