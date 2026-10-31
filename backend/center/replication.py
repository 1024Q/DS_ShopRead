"""Incremental publication of durable non-browser state from the collector."""

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


@contextmanager
def connect(path):
    path = Path(path).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        if path.name == "business.sqlite3":
            node = path.parents[2] / "shops.sqlite3"
            db.execute("ATTACH DATABASE ? AS node", (node.as_uri() + "?mode=ro",))
            db.execute("CREATE TEMP VIEW shops AS SELECT * FROM node.shops")
        yield db


def record(shop, scope, payload, updated):
    # Payload stays the stored JSON text; it is forwarded without a loads/dumps round trip.
    return {"shop": str(shop), "scope": scope, "payload": payload, "updated": updated}


def fingerprint(payload):
    return hashlib.sha256(
        (payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, sort_keys=True)).encode()
    ).hexdigest()[:24]


def encode(records):
    """JSON request body; stored payload text is embedded as-is."""
    return (
        '{"records":['
        + ",".join(
            '{"shop":%s,"scope":%s,"updated":%s,"payload":%s}'
            % (
                json.dumps(r["shop"]),
                json.dumps(r["scope"]),
                json.dumps(r["updated"]),
                r["payload"] if isinstance(r["payload"], str) else json.dumps(r["payload"], ensure_ascii=False),
            )
            for r in records
        )
        + "]}"
    ).encode()
