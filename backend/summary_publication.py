"""Publish verified homepage metrics without replacing an unfinished detail snapshot."""

import contextvars
import copy
import json
from contextlib import contextmanager

KINDS = {"ad_reports", "transactions"}
_handler = contextvars.ContextVar("homepage_summary_publisher", default=None)
_summary_only = contextvars.ContextVar("homepage_summary_only", default=False)


@contextmanager
def context(handler, *, summary_only=False):
    token = _handler.set(handler)
    mode = _summary_only.set(summary_only)
    try:
        yield
    finally:
        _summary_only.reset(mode)
        _handler.reset(token)


def scheduled(kind, options, platform="pdd", force=False):
    """An execution policy, never part of the existing cache/frequency identity."""
    return platform == "pdd" and kind in KINDS and options.get("trade_range") == "realtime" and not force


def only(kind, options):
    return _summary_only.get() and kind in KINDS and options.get("trade_range") == "realtime"


def manual_details_pending(store, shop, cache_key):
    """An interrupted explicit full read may retry without its in-memory force flag."""
    with store.connect() as db:
        row = db.execute(
            "SELECT json_extract(payload,'$.force_full') FROM collection_checkpoints WHERE shop_id=? AND cache_key=?",
            (shop, cache_key),
        ).fetchone()
    return bool(row and row[0])


async def publish(kind, options, payload):
    handler = _handler.get()
    if handler and kind in KINDS and options.get("trade_range", "realtime") == "realtime":
        if payload.get("summary_complete") is not True or not payload.get("metrics"):
            raise ValueError("首页摘要尚未完整，不能发布")
        await handler(kind, options, copy.deepcopy(payload))


def key(cache_key):
    return "summary:" + cache_key


def merge(kind, full, summary):
    """Keep detail rows and their clock separate from newly verified metrics."""
    if kind not in KINDS or not summary or not summary["payload"].get("summary_complete"):
        return full
    if full and full["updated"] >= summary["updated"]:
        return full
    payload = dict(full["payload"]) if full else {"rows": [], "complete": False}
    brief = summary["payload"]
    payload.update(
        metrics={**payload.get("metrics", {}), **brief["metrics"]},
        summary_complete=True,
        details_pending=True,
        details_on_demand=bool(brief.get("details_on_demand")),
        details_complete=bool(full and full["payload"].get("complete")),
        complete=False,
        summary_updated_at=summary["updated"],
        details_updated_at=full["updated"] if full else None,
        _sync=brief.get("_sync", {}),
    )
    if kind == "transactions":
        payload["rows"] = brief.get("rows", [])
    if brief.get("_center_received_at"):
        payload["_center_received_at"] = brief["_center_received_at"]
    return {**(full or summary), "payload": payload, "updated": summary["updated"]}


def read_many(store, keys):
    from .partition_reads import partitions, read_connection

    result = {}
    for sid in partitions(store, keys):
        pairs = [(shop, key(value)) for shop, value in keys.items() if sid is None or shop == sid]
        with read_connection(store, sid) as db:
            for offset in range(0, len(pairs), 200):
                part = pairs[offset : offset + 200]
                if not part:
                    continue
                sql = "WITH wanted(shop_id,cache_key) AS (VALUES " + ",".join("(?,?)" for _ in part) + ") "
                for row in db.execute(
                    sql + "SELECT c.* FROM business_cache c JOIN wanted w USING(shop_id,cache_key)",
                    [value for pair in part for value in pair],
                ):
                    result[row["shop_id"], row["cache_key"][8:]] = {
                        "payload": json.loads(row["payload"]),
                        "updated": row["updated"],
                    }
    return result
