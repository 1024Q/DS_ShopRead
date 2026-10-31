"""Sort the entire filtered result before pagination or export."""

from decimal import Decimal
import re


def sort_rows(rows, field, direction="asc"):
    if not field:
        return rows
    if direction not in ("asc", "desc") or not re.fullmatch(r"[a-z_][a-z0-9_]{0,59}", field):
        raise ValueError("排序条件无效")

    def value(row):
        v = row.get(field)
        if v is None or v == "":
            return None
        if isinstance(v, (dict, list)):
            return str(v)
        return v

    def key(row):
        v = value(row)
        if isinstance(v, (int, float)):
            return (0, Decimal(str(v)))
        return (1, str(v).casefold())

    present = [r for r in rows if value(r) is not None]
    absent = [r for r in rows if value(r) is None]
    return sorted(present, key=key, reverse=direction == "desc") + absent
