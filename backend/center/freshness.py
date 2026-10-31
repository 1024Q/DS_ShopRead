"""Use the reporting node's actual cadence without inventing a fleet-wide interval."""

import hashlib
import json
import time
from sqlalchemy import select
from .database import snapshots


def configurations(database):
    scope = "response:" + hashlib.sha256(b"system/settings?").hexdigest()
    with database.engine.connect() as db:
        rows = db.execute(
            select(snapshots.c.node, snapshots.c.scope, snapshots.c.payload).where(
                snapshots.c.shop == "0", snapshots.c.scope.in_(["collector_status", scope])
            )
        ).all()
    result = {}
    for node, kind, payload in sorted(rows, key=lambda r: r[1] == "collector_status"):
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        config = payload.get("collection") if kind == "collector_status" else payload.get("value", {}).get("collection")
        if isinstance(config, dict) and config:
            result[node] = config
    return result


def describe(kind, options, payload, updated, config, platform, now=None):
    from ..collection import due, mode, effective_interval

    now = time.time() if now is None else now
    sync = payload.get("_sync", {})
    checked = sync.get("full_snapshot_updated_at", updated)
    age = max(0, round(now - checked)) if checked else None
    if not config:
        return {"age_seconds": age, "frequency_known": False, "freshness": "unknown", "overdue": False}
    paused = not config.get("enabled", True) or mode(kind, options, config, platform) == "off"
    overdue = bool(not paused and due(kind, options, checked, config, now, platform))
    return {
        "age_seconds": age,
        "frequency_known": True,
        "freshness": "paused" if paused else "overdue" if overdue else "current",
        "overdue": overdue,
        "interval_seconds": effective_interval(kind, options, config, platform),
    }
