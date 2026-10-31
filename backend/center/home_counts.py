"""Bounded, revision-fenced caches of small readonly homepage list counters."""

import copy
import json
import threading
import time
from collections import Counter, OrderedDict
from concurrent.futures import Future
from sqlalchemy import or_, select, tuple_
from .database import snapshots


class CountCache:
    def __init__(self):
        self.entries = OrderedDict()
        self.pending = {}
        self.lock = threading.Lock()

    def read(self, key, revision, loader, current_revision):
        token = (key, revision)
        with self.lock:
            now = time.monotonic()
            for old in list(self.entries):
                if self.entries[old][0] <= now:
                    del self.entries[old]
            saved = self.entries.get(key)
            if saved and saved[1] == revision:
                self.entries.move_to_end(key)
                return copy.deepcopy(saved[2])
            future = self.pending.get(token)
            owner = future is None
            if owner:
                future = self.pending[token] = Future()
        if not owner:
            return copy.deepcopy(future.result())
        try:
            data = loader()
            # Never retain a projection spanning a concurrent upload.
            if current_revision() == revision:
                with self.lock:
                    self.entries[key] = (time.monotonic() + 30, revision, data)
                    self.entries.move_to_end(key)
                    while len(self.entries) > 64:
                        self.entries.popitem(last=False)
            future.set_result(data)
            return copy.deepcopy(data)
        except BaseException as error:
            future.set_exception(error)
            raise
        finally:
            with self.lock:
                self.pending.pop(token, None)


def signature(database, kind, shops):
    pairs = sorted({(s["node_id"], str(s["local_id"])) for s in shops})
    if not pairs:
        return ()
    prefixes = [
        kind + ":",
        "archive:" + kind + ":",
        "resolved:" + kind + ":",
        "freshness:" + kind + ":",
        "freshness:summary:" + kind + ":",
    ]
    scope = or_(
        snapshots.c.scope.in_(["attention:" + kind]),
        *(snapshots.c.scope.like(p.replace("_", r"\_") + "%", escape="\\") for p in prefixes),
    )
    result = []
    with database.engine.connect() as db:
        for offset in range(0, len(pairs), 300):
            result.extend(
                db.execute(
                    select(snapshots.c.node, snapshots.c.shop, snapshots.c.scope, snapshots.c.updated).where(
                        tuple_(snapshots.c.node, snapshots.c.shop).in_(pairs[offset : offset + 300]), scope
                    )
                ).all()
            )
        if kind == "orders":
            # V2 order rows arrive independently from their scope metadata.
            result.extend(
                db.execute(
                    select(snapshots.c.node, snapshots.c.shop, snapshots.c.scope, snapshots.c.updated).where(
                        snapshots.c.node.in_({s["node_id"] for s in shops}),
                        snapshots.c.shop == "0",
                        snapshots.c.scope == "order-change-clock",
                    )
                ).all()
            )
    return tuple(sorted(tuple(row) for row in result))


def read(service, kind, options, shop_id, platform, filters, page_size):
    from ..business import filter_rows
    from ..filter_scope import matches
    from ..platform_support import supports

    shops = [
        s
        for s in service.store.list()
        if matches(shop_id, s["id"])
        and matches(platform, s.get("platform", "pdd"))
        and supports(s.get("platform", "pdd"), kind)
    ]
    # Ownership, identity, status and calendar scope are all part of the key.
    key = json.dumps([kind, options, shops, filters, page_size], sort_keys=True, separators=(",", ":"))
    current = lambda: signature(service.database, kind, shops)

    def load():
        data = service.data(kind, options, shop_id=shop_id, platform=platform, read_only=True)
        rows = filter_rows(data["rows"], kind, **filters)
        counts = dict(Counter(row["shop_id"] for row in rows))
        return {
            **data,
            "rows": [],
            "metrics": [],
            "total": len(rows),
            "shop_counts": counts,
            "priority_count": sum(bool(row.get("priority")) for row in rows),
            "statuses": [],
            "page_count": max(1, (len(rows) + page_size - 1) // page_size),
            "_home_counted": True,
        }

    return service._home_count_cache.read(key, current(), load, current)
