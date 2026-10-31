"""Business revisions exclude node heartbeats and carry scoped change notifications."""

import hashlib
import json
from sqlalchemy import select, case
from .database import snapshots


def heads(database):
    with database.engine.connect() as db:
        rows = db.execute(
            select(
                snapshots.c.node,
                snapshots.c.shop,
                snapshots.c.scope,
                snapshots.c.updated,
                case((snapshots.c.scope == "shop_metadata", snapshots.c.payload), else_=None),
            ).where(snapshots.c.scope != "collector_status", ~snapshots.c.scope.like("response:%"))
        ).all()
    return {
        (node, shop, scope): hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if scope == "shop_metadata"
        else updated
        for node, shop, scope, updated, payload in rows
    }


def revision(current):
    return hashlib.sha256(
        json.dumps(sorted((*key, stamp) for key, stamp in current.items()), separators=(",", ":")).encode()
    ).hexdigest()


def notification(previous, current, shops):
    version = revision(current)
    if previous is None:
        return {"version": version, "reset": True, "changes": []}
    changed = {key for key in current.keys() | previous.keys() if current.get(key) != previous.get(key)}
    mapping = {(s["node_id"], str(s["local_id"])): s for s in shops}
    result = {}
    for node, local, scope in changed:
        shop = mapping.get((node, local))
        if scope.startswith("freshness:"):
            scope = scope[10:]
        parts = scope.split(":")
        kind = (
            parts[1]
            if parts[0] in ("attention", "archive", "history", "resolved", "summary") and len(parts) > 1
            else parts[0]
        )
        if kind == "order-change-clock":
            kind = "orders"
        elif kind == "tax_record":
            kind = "tax"
        elif kind == "shop_metadata":
            kind = "*"
        row = {"kind": kind}
        if shop:
            row.update(shop_id=shop["id"], platform=shop.get("platform", "pdd"))
        result[(kind, row.get("shop_id"))] = row
    return {"version": version, "reset": False, "changes": list(result.values())}
