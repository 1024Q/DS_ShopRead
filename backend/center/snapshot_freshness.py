"""Lightweight repeated-observation records, separate from immutable content stamps."""

import hashlib
import json


def prepare(outbox, rows):
    wire = []
    for row in rows:
        # Only live business snapshots are deduplicated. Financial/archive and
        # attention reconciliation semantics continue through their own lanes.
        if not outbox or outbox.lane != "business":
            wire.append(row)
            continue
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        content = {k: v for k, v in payload.items() if k not in ("_sync", "_center_received_at")}
        fingerprint = hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        row["_content_hash"] = fingerprint
        saved = outbox.content(row["shop"], row["scope"])
        if saved and saved[0] == fingerprint and saved[2] >= saved[1] and saved[1] > 0:
            row["_content_updated"] = saved[1]
            wire.append(
                {
                    **row,
                    "scope": "freshness:" + row["scope"],
                    "payload": {
                        "scope": row["scope"],
                        "content_updated": saved[1],
                        "collected_at": row["updated"],
                        "_sync": payload.get("_sync", {}),
                    },
                }
            )
        else:
            row["_content_updated"] = row["updated"]
            wire.append(row)
    return wire


def overlay(database, rows):
    """Overlay observation metadata only when it refers to this exact content."""
    from .database import snapshots
    from sqlalchemy import select, tuple_

    if not rows:
        return rows
    keys = [(r["node"], r["shop"], "freshness:" + r["scope"]) for r in rows]
    fresh = {}
    with database.engine.connect() as db:
        for start in range(0, len(keys), 250):
            for row in db.execute(
                select(snapshots).where(
                    tuple_(snapshots.c.node, snapshots.c.shop, snapshots.c.scope).in_(keys[start : start + 250])
                )
            ).mappings():
                fresh[(row["node"], row["shop"], row["scope"][10:])] = row
    result = []
    for row in rows:
        value = fresh.get((row["node"], row["shop"], row["scope"]))
        if value and value["payload"].get("content_updated") == row["updated"] and value["updated"] > row["updated"]:
            payload = {
                **row["payload"],
                "_sync": value["payload"].get("_sync", {}),
                "_center_received_at": value["payload"].get("_center_received_at", value["updated"]),
            }
            row = {**row, "updated": value["updated"], "payload": payload}
        result.append(row)
    return result
