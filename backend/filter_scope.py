"""Multi-value query scope, retaining legacy scalar callers and SQL parameters."""


def values(value):
    return tuple(sorted({part.strip() for part in str(value or "").split(",") if part.strip()}))


def matches(scope, value):
    selected = values(scope)
    return not selected or str(value) in selected


def sql_filter(column, scope, args):
    selected = values(scope)
    args.extend(selected)
    return column + " IN (" + ",".join("?" for _ in selected) + ")"
