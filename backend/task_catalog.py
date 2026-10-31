"""User-facing task groups; keep member identities for checkpoints and rollback."""

REPORT_KINDS = ("ad_products", "ad_reports", "goodsdata", "transactions")
REMOVED = {"orders@base", "orders@history", "orders@daily", *[k + "@quarter" for k in REPORT_KINDS]}
HOME = {
    "overview@base",
    "transactions@base",
    "ad_reports@base",
    "orders@refund",
    "orders@shipping_due",
    "aftersales@expiring",
    "products@base",
    *[k + "@attention" for k in ("aftersales", "evidence", "intercepts", "packages", "invoices", "tickets")],
}
LEGACY_GROUPS = {
    "aftersales@service": ("aftersales@base", "aftersales@merchant_pending"),
    "reconciliation_day@combined": ("reconciliation_day@base", "reconciliation_month@base"),
    **{kind + "@history": tuple(kind + "@" + span for span in ("yesterday", "week", "month")) for kind in REPORT_KINDS},
}
GROUPS = {
    "overview@home": ("首页数据综合采集", tuple(sorted(HOME - {"products@base"}))),
    "transactions@reports": (
        "历史报表与对账综合采集",
        tuple(
            member for legacy, members in LEGACY_GROUPS.items() if legacy != "aftersales@service" for member in members
        ),
    ),
    "goodsdata@live": ("商品实时经营数据", ("ad_products@base", "goodsdata@base")),
    "orders@unfinished": ("订单查询-未完结订单回查", ("orders@unpaid", "orders@unshipped", "orders@unreceived")),
    "orders@recent": ("订单查询-今昨订单", ("orders@today", "orders@yesterday")),
    "tax@routine": (
        "日常业务综合采集",
        tuple(
            kind + "@base"
            for kind in (
                "activities",
                "ad_logs",
                "evidence",
                "intercepts",
                "packages",
                "invoices",
                "reviews",
                "tickets",
                "tax",
            )
        ),
    ),
    "aftersales@work": ("售后与工单待办", ("aftersales@base", "aftersales@merchant_pending", "tickets@pending")),
    "navigator@diagnostics": ("商品诊断与权益", ("navigator@base", "quality@base", "brands@base")),
}


def removed_report(kind, scope):
    return kind in REPORT_KINDS and scope.get("trade_range") == "quarter"


def grouped_id(identifier):
    return next((key for key, (_, members) in GROUPS.items() if identifier in members), identifier)


def decorate(entries):
    from .export_labels import MODULES

    result = []
    seen = set()
    for item in entries:
        identifier = item["id"]
        if identifier in REMOVED:
            continue
        target = grouped_id(identifier)
        if target in seen:
            continue
        seen.add(target)
        if target in GROUPS:
            name, members = GROUPS[target]
            result.append(
                {"id": target, "kind": target.split("@")[0], "name": name, "label": name, "members": list(members)}
            )
        else:
            label = MODULES.get(item["kind"], item["kind"]) + "-" + item["name"]
            result.append({**item, "label": ("首页-" if identifier in HOME else "") + label, "members": [identifier]})
    return result


def assign(config, platform, identifier, target):
    assignments = config.setdefault("task_modes", {}).setdefault(platform, {})
    assignments[identifier] = target
    # Keep legacy member assignments in sync for safe code rollback.
    for member in GROUPS.get(identifier, ("", (identifier,)))[1]:
        assignments[member] = target
    members = set(GROUPS.get(identifier, ("", ()))[1])
    for legacy, children in LEGACY_GROUPS.items():
        if set(children) <= members:
            assignments[legacy] = target


def expand_legacy(config):
    """Preserve the effective legacy group values before replacing their controls."""
    for assignments in config.get("task_modes", {}).values():
        for legacy, members in LEGACY_GROUPS.items():
            if legacy in assignments:
                for member in members:
                    assignments[member] = assignments[legacy]
    return config


def title(kind, scope):
    from .frequency_policy import category, CATEGORIES
    from .export_labels import MODULES

    cat = category(kind, scope)
    identifier = kind + "@" + cat
    target = grouped_id(identifier)
    name = MODULES.get(kind, kind) + "-" + CATEGORIES.get(kind, {}).get(cat, CATEGORIES["__shared__"].get(cat, cat))
    if target in GROUPS:
        return GROUPS[target][0] + " / " + name
    return ("首页-" if identifier in HOME else "") + name
