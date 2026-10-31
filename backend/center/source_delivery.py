"""Project local publication and center acknowledgements without reading business payloads."""

import json
import sqlite3
from contextlib import closing
from .replication_worker import Delivery
from ..collector_update import read_json


def load(directory):
    result = {
        lane: read_json(directory / f"center-delivery-{lane}.json", {}) for lane in ("business", "auxiliary", "orders")
    }
    path = directory / ("business.sqlite3" if (directory / "business.sqlite3").exists() else "shops.sqlite3")
    for lane in ("business", "auxiliary"):
        if result[lane].get("outbox") or result[lane].get("per_shop"):
            manifest = (
                read_json(directory / "shop-storage.json", {}).get("shops", {})
                if result[lane].get("per_shop")
                else None
            )
            for name, column in (
                ("acks", "ack"),
                ("confirmed", "confirmed"),
                ("retries", "retries"),
                ("rejected", "rejected"),
            ):
                result[lane][name] = ReceiptLookup(path, lane, column, directory, manifest)
    return result


class ReceiptLookup:
    """Look up only the visible scopes, without loading a growing receipt file."""

    def __init__(self, path, lane, column, directory=None, manifest=None):
        self.path, self.lane, self.column = path, lane, column
        self.directory, self.manifest = directory, manifest
        self.cache = {}

    def get(self, key, default=None):
        if key not in self.cache:
            shop, scope = json.loads(key)
            path = self.path
            if self.manifest is not None:
                entry = self.manifest.get(str(shop), {})
                if not entry.get("path") or entry.get("state") != "active":
                    return default
                path = self.directory / entry["path"]
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='delivery_outbox'").fetchone():
                    return default
                column = (
                    "attempts,retry_at,error,rejected,updated"
                    if self.column in ("retries", "rejected")
                    else self.column
                )
                row = db.execute(
                    f"SELECT {column} FROM delivery_outbox WHERE lane=? AND shop_id=? AND scope=?",
                    (self.lane, shop, scope),
                ).fetchone()
            if self.column in ("retries", "rejected"):
                self.cache[key] = (
                    dict(zip(("attempts", "retry_at", "error", "rejected", "updated"), row))
                    if row and row[0] and bool(row[3]) == (self.column == "rejected")
                    else default
                )
            else:
                self.cache[key] = row[0] if row else default
        return self.cache[key]


def describe(saved, shop, key, cache, scope, checkpoint, order_view=None):
    local = (
        (order_view or {}).get("updated", 0)
        if scope.get("_order_lane") and (order_view or {}).get("order_stream")
        else cache.get("updated", 0)
    )
    result = {"local_at": local or None, "confirmed_at": None, "retry_at": None, "attempts": 0, "error": ""}
    if not local:
        return {**result, "state": "checkpoint" if checkpoint.get("loaded") else "unpublished"}
    origin = cache.get("delivery_origin", cache.get("origin"))
    lane = "auxiliary" if scope.get("_attention") or origin == "daily_archive" else "business"
    remote_key = (
        "attention:" + key.split(":")[0]
        if scope.get("_attention")
        else ("archive:" if origin == "daily_archive" else "") + cache.get("delivery_key", key)
    )
    targets = [(lane, remote_key, local)]
    if key.startswith("orders:") and scope.get("_order_lane") and order_view:
        if order_view.get("order_stream"):
            orders = saved.get("orders", {})
            if (
                orders.get("stream") != order_view["order_stream"]
                or orders.get("confirmed_revision", 0) < order_view["order_revision"]
            ):
                return {
                    **result,
                    "local_at": order_view["updated"],
                    "state": "failed" if orders.get("error") else "pending",
                    "error": orders.get("error", ""),
                    "retry_at": orders.get("retry_at"),
                }
            targets = [("business", order_view["cache_key"], order_view["updated"])]
        else:
            targets.append(("business", order_view["cache_key"], order_view["updated"]))
    pending = []
    confirmed = []
    for lane, remote_key, stamp in targets:
        state = saved.get(lane, {})
        ident = Delivery.key(shop, remote_key)
        if state.get("acks", {}).get(ident, 0) >= stamp:
            confirmed.append(state.get("confirmed", {}).get(ident))
            continue
        failure = state.get("retries", {}).get(ident, {})
        rejected = state.get("rejected", {}).get(ident, {})
        if rejected and rejected.get("updated") == stamp:
            failure = {**rejected, "error": "中心拒收，等待数据更新后重试：" + rejected.get("error", "")}
        inflight = state.get("inflight", {}).get(ident, {})
        pending.append(
            {
                **result,
                "state": "uploading" if inflight.get("updated", 0) >= stamp else "failed" if failure else "pending",
                "retry_at": failure.get("retry_at"),
                "attempts": failure.get("attempts", 0),
                "error": failure.get("error", ""),
            }
        )
    if pending:
        return max(pending, key=lambda p: {"pending": 0, "uploading": 1, "failed": 2}[p["state"]])
    confirmed_at = max((v for v in confirmed if v), default=None)
    return {**result, "state": "confirmed", "confirmed_at": confirmed_at,
            "upload_seconds": max(0, confirmed_at - local) if confirmed_at else None}
