"""Project cached node coverage onto the center's current shop ownership."""

from ..filter_scope import matches
from ..shop_identity import identity
import time


def freshness(name, updated, online, error=None, refreshing=False, now=None):
    age = max(0, round((time.time() if now is None else now) - updated))
    return {
        "node_name": name,
        "updated_at": updated,
        "age_seconds": age,
        "stale": age > 30 or not online or bool(error),
        "online": online,
        "refreshing": refreshing,
        "error": (error or {}).get("message", ""),
        "error_at": (error or {}).get("at"),
    }


def project(value, shops, node, params):
    owned = {
        s["local_id"]: s
        for s in shops
        if s["node_id"] == node
        and matches(params.get("shop_id", ""), s["id"])
        and matches(params.get("platform", ""), s.get("platform", "pdd"))
    }
    rows = {}
    for row in value.get("rows", []):
        shop = owned.get(row.get("shop_id"))
        if shop is None:
            continue  # Never interpret a retired local ID as a global shop ID.
        rows[shop["id"]] = {**row, **identity(shop), "shop_id": shop["id"]}
    for shop in owned.values():
        if shop["id"] not in rows:
            rows[shop["id"]] = {
                **identity(shop),
                "shop_id": shop["id"],
                "status": "waiting",
                "updated": 0,
                "total": 0,
                "overdue_modules": 0,
                "daily_updated": 0,
                "daily_total": 0,
                "modules": [],
                "last_updated": None,
                "last_collected": None,
            }
    rows = list(rows.values())
    result = {
        **value,
        "rows": rows,
        "updated_shops": sum(r.get("total", 0) > 0 and r.get("updated", 0) == r["total"] for r in rows),
        "total_shops": sum(r.get("total", 0) > 0 for r in rows),
        "excluded_shops": sum(not r.get("total", 0) for r in rows),
    }
    for target, field in [
        ("updated_modules", "updated"),
        ("total_modules", "total"),
        ("overdue_modules", "overdue_modules"),
        ("daily_updated", "daily_updated"),
        ("daily_total", "daily_total"),
    ]:
        result[target] = sum(r.get(field, 0) for r in rows)
    for field in ("last_updated", "last_collected"):
        result[field] = max((r[field] for r in rows if r.get(field)), default=None)
    return result
