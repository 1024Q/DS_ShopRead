"""Explicit partition-local reads for hot paths; no SQL rewriting or full-table copies.

Callers must merge global ordering themselves. Aggregates are safe only when grouped
by shop_id. Arbitrary SQL keeps using FederatedConnection's compatibility path.
"""

from contextlib import contextmanager
import sqlite3
from .shard_connection import shop_context


def partitions(store, shop_ids=None):
    manager = getattr(store, "_shards", None)
    if not manager:
        return [None]
    wanted = None if shop_ids is None else set(shop_ids)
    return [sid for sid in list(manager.paths) if wanted is None or sid in wanted]


@contextmanager
def read_connection(store, shop_id=None):
    token = shop_context.set(shop_id)
    try:
        with store.connect() as db:
            # A read snapshot must not enter the database's writer admission gate.
            raw = db.db(shop_id) if getattr(db, "manager", None) else db
            if isinstance(raw, sqlite3.Connection) and not raw.in_transaction:
                sqlite3.Connection.execute(raw, "BEGIN")
            yield db
    finally:
        shop_context.reset(token)


def rows(store, sql, parameters=(), shop_ids=None):
    for sid in partitions(store, shop_ids):
        with read_connection(store, sid) as db:
            yield from db.execute(sql, parameters)
