"""Order identity storage, durable change revisions and incremental UI projection."""

import json, time, uuid, sqlite3
from datetime import datetime, timedelta
from .business_reader import BEIJING


def initialize(store):
    with store.connect() as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS order_entities(
          ordinal INTEGER PRIMARY KEY AUTOINCREMENT,shop_id INTEGER,cache_key TEXT,id TEXT,
          payload TEXT,observed REAL,revision INTEGER,created_at TEXT,status TEXT,
          UNIQUE(shop_id,cache_key,id));
          CREATE INDEX IF NOT EXISTS order_entity_revision ON order_entities(revision);
          CREATE INDEX IF NOT EXISTS order_entity_dates ON order_entities(shop_id,cache_key,created_at,status);
          CREATE TABLE IF NOT EXISTS order_clock(id INTEGER PRIMARY KEY,stream TEXT,value INTEGER);
        """)
        db.execute("INSERT OR IGNORE INTO order_clock VALUES(1,?,0)", (uuid.uuid4().hex,))


def put(db, shop, key, original, stamp, deleted=False):
    from .order_plan import normalize

    row = normalize(original)
    rid = str(row["id"])
    old = db.execute(
        "SELECT * FROM order_entities WHERE shop_id=? AND cache_key=? AND id=?", (shop, key, rid)
    ).fetchone()
    if old and old["observed"] > stamp:
        return False
    row.pop("_order_observed", None)
    encoded = None if deleted else json.dumps(row, ensure_ascii=False, sort_keys=True)
    if old and old["payload"] == encoded:
        if old["observed"] != stamp:
            db.execute("UPDATE order_entities SET observed=? WHERE ordinal=?", (stamp, old["ordinal"]))
        return False
    revision = db.execute("UPDATE order_clock SET value=value+1 WHERE id=1 RETURNING value").fetchone()[0]
    db.execute(
        """INSERT INTO order_entities(shop_id,cache_key,id,payload,observed,revision,created_at,status) VALUES(?,?,?,?,?,?,?,?)
      ON CONFLICT(shop_id,cache_key,id) DO UPDATE SET payload=excluded.payload,observed=excluded.observed,revision=excluded.revision,created_at=excluded.created_at,status=excluded.status""",
        (shop, key, rid, encoded, stamp, revision, row.get("created_at", ""), row.get("attention_status", "")),
    )
    ordinal = (
        old["ordinal"]
        if old
        else db.execute(
            "SELECT ordinal FROM order_entities WHERE shop_id=? AND cache_key=? AND id=?", (shop, key, rid)
        ).fetchone()[0]
    )
    if deleted:
        db.execute("DELETE FROM table_rows WHERE shop_id=? AND cache_key=? AND ordinal=?", (shop, key, ordinal))
        return True
    project(db, shop, key, ordinal, row)
    return True


def project(db, shop, key, ordinal, row):
    from .table_index import SEARCH
    from .operations import decorate
    from .product_images import thumbnail

    row = decorate("orders", row, "pdd")
    db.execute(
        """INSERT OR REPLACE INTO table_rows(shop_id,cache_key,ordinal,payload,status,created_at,priority,name,goods_id,sales,visitors,amount,stock,search_text,thumbnail_url)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            shop,
            key,
            ordinal,
            json.dumps(row, ensure_ascii=False),
            row.get("status", ""),
            row.get("created_at", ""),
            row.get("priority", 0),
            row.get("name", ""),
            str(row.get("goods_id", "")),
            None,
            None,
            row.get("amount"),
            None,
            " ".join(str(row.get(k, "")) for k in SEARCH).casefold(),
            thumbnail(row),
        ),
    )


def rebuild(db, shop, key, payload, updated):
    db.execute("DELETE FROM table_rows WHERE shop_id=? AND cache_key=?", (shop, key))
    count = 0
    for row in db.execute(
        "SELECT ordinal,payload FROM order_entities WHERE shop_id=? AND cache_key=? AND payload IS NOT NULL",
        (shop, key),
    ).fetchall():
        project(db, shop, key, row["ordinal"], json.loads(row["payload"]))
        count += 1
    db.execute(
        "INSERT OR REPLACE INTO table_snapshots VALUES(?,?,?,?,?)",
        (shop, key, updated, json.dumps(payload, ensure_ascii=False), count),
    )


def publish(store, shop, scope, payload, partial=False):
    # Replay retained rows only; committed chunks are fenced by observation time.
    for attempt in range(3):
        try:
            return _publish(store, shop, scope, payload, partial)
        except sqlite3.OperationalError as error:
            if not any(w in str(error).lower() for w in ("locked", "busy")) or attempt == 2:
                raise
            time.sleep(0.25 * (attempt + 1))


def _publish(store, shop, scope, payload, partial=False):
    from .order_plan import key, seed

    cache = key(shop)
    sid = shop["id"]
    stamp = payload.get("_sync", {}).get("snapshot_started_at", time.time())
    with store.connect() as db:
        existing = db.execute(
            "SELECT payload FROM business_cache WHERE shop_id=? AND cache_key=?", (sid, cache)
        ).fetchone()
    previous = json.loads(existing[0]) if existing else seed(store, shop)
    if not previous.get("_order_rows_v2"):
        # One-time migration yields the shared writer between small batches.
        # Observations fence a second collector or a resumed migration from
        # replacing pages already committed with fresher information.
        rows = previous.get("rows", [])
        for offset in range(0, len(rows), 200):
            with store.write_connection() as db:
                active = db.execute("SELECT profile_version FROM shops WHERE id=?", (sid,)).fetchone()
                if not active or active[0] != shop.get("profile_version", 0):
                    return
                for row in rows[offset : offset + 200]:
                    put(db, sid, cache, row, row.get("_order_observed", 0))
    incoming_rows = list(payload.get("rows", []))
    for offset in range(0, len(incoming_rows), 100):
        with store.write_connection() as db:
            active = db.execute("SELECT profile_version FROM shops WHERE id=?", (sid,)).fetchone()
            if not active or active[0] != shop.get("profile_version", 0):
                return
            for row in incoming_rows[offset : offset + 100]:
                put(db, sid, cache, {**row, "_order_status_stale": False}, stamp)
    # Expired history maintenance has a bounded per-publication budget.
    floor = (datetime.now(BEIJING).date() - timedelta(days=89)).isoformat()
    with store.write_connection() as db:
        expired = db.execute(
            "SELECT payload,observed FROM order_entities WHERE shop_id=? AND cache_key=? AND payload IS NOT NULL AND created_at<>'' AND created_at<? LIMIT 100",
            (sid, cache, floor),
        ).fetchall()
        for saved in expired:
            put(db, sid, cache, json.loads(saved["payload"]), max(stamp, saved["observed"]), deleted=True)
    incoming = {str(r["id"]) for r in incoming_rows}
    new_pending = set()
    lane = scope.get("_order_lane")
    first = scope["date_from"]
    if not partial:
        lower = datetime.fromtimestamp(scope["start"], BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        upper = datetime.fromtimestamp(scope["end"], BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        with store.connect() as db:
            candidates = db.execute(
                "SELECT id FROM order_entities WHERE shop_id=? AND cache_key=? AND payload IS NOT NULL AND created_at>=? AND created_at<=?",
                (sid, cache, lower, upper),
            ).fetchall()
        missing = [r["id"] for r in candidates if r["id"] not in incoming]
        for offset in range(0, len(missing), 100):
            with store.write_connection() as db:
                active = db.execute("SELECT profile_version FROM shops WHERE id=?", (sid,)).fetchone()
                if not active or active[0] != shop.get("profile_version", 0):
                    return
                for rid in missing[offset : offset + 100]:
                    saved = db.execute(
                        "SELECT payload,observed FROM order_entities WHERE shop_id=? AND cache_key=? AND id=?",
                        (sid, cache, rid),
                    ).fetchone()
                    if not saved or not saved["payload"] or saved["observed"] > stamp:
                        continue
                    row = json.loads(saved["payload"])
                    if lane == "open":
                        if row.get("attention_status") != scope["status"]:
                            continue
                        row["_order_status_stale"] = True
                        new_pending.add(str(row.get("created_at", ""))[:10])
                        put(db, sid, cache, row, stamp)
                    else:
                        put(db, sid, cache, row, stamp, deleted=True)
    with store.write_connection() as db:
        active = db.execute("SELECT profile_version FROM shops WHERE id=?", (sid,)).fetchone()
        if not active or active[0] != shop.get("profile_version", 0):
            return
        # Re-read under the writer lock: concurrent pages must not lose coverage.
        latest = db.execute(
            "SELECT payload FROM business_cache WHERE shop_id=? AND cache_key=?", (sid, cache)
        ).fetchone()
        old = json.loads(latest[0]) if latest else previous
        days = dict(old.get("_order_days", {}))
        pending = set(old.get("_order_recheck_days", []))
        pending.update(new_pending)
        if not partial and lane != "open" and scope["end"] - scope["start"] >= 86399:
            days[first] = max(days.get(first, 0), stamp)
        if not partial and lane != "open":
            remaining = db.execute(
                "SELECT 1 FROM order_entities WHERE shop_id=? AND cache_key=? AND payload IS NOT NULL AND created_at>=? AND created_at<=? AND json_extract(payload,'$._order_status_stale')=1 LIMIT 1",
                (sid, cache, first, first + "\uffff"),
            ).fetchone()
            if not remaining:
                pending.discard(first)
        today = datetime.now(BEIJING).date()
        floor = (today - timedelta(days=89)).isoformat()
        days = {d: v for d, v in days.items() if floor <= d <= today.isoformat()}
        count = db.execute(
            "SELECT COUNT(*) FROM order_entities WHERE shop_id=? AND cache_key=? AND payload IS NOT NULL", (sid, cache)
        ).fetchone()[0]
        clock = db.execute("SELECT * FROM order_clock WHERE id=1").fetchone()
        updated = time.time()
        meta = {
            "_order_rows_v2": True,
            "_order_stream": clock["stream"],
            "_order_revision": clock["value"],
            "_order_days": days,
            "_order_recheck_days": sorted(d for d in pending if d >= floor),
            "complete": len(days) >= 90,
            "source_total": count,
            "source_name": f"订单增量库 · 已覆盖 {len(days)}/90 天 · 已入库 {count} 单",
            "scope_note": "分页结果已入库；完整日期段才计入覆盖，中心确认后完成回传",
            "_sync": {
                "collection_mode": "incremental",
                "snapshot_started_at": max(stamp, old.get("_sync", {}).get("snapshot_started_at", 0)),
            },
        }
        text = json.dumps(meta, ensure_ascii=False)
        db.execute(
            "INSERT OR REPLACE INTO business_cache(shop_id,cache_key,payload,updated) VALUES(?,?,?,?)",
            (sid, cache, text, updated),
        )
        db.execute("INSERT OR REPLACE INTO table_snapshots VALUES(?,?,?,?,?)", (sid, cache, updated, text, count))
        store.invalidate_responses(sid, "orders", db=db)
    from .status_cache import touch

    touch(store, sid)
    from .center.delivery_notify import notify

    notify(store.directory, sid)


def recheck_windows(store, shop, scope):
    from .order_plan import key

    with store.connect() as db:
        rows = db.execute(
            "SELECT created_at FROM order_entities WHERE shop_id=? AND cache_key=? AND payload IS NOT NULL AND json_extract(payload,'$._order_status_stale')=1 AND created_at>=? AND created_at<=?",
            (shop["id"], key(shop), scope["date_from"], scope["date_to"] + "\uffff"),
        ).fetchall()
    windows = hour_windows(rows, scope)
    return windows or [(scope["start"], scope["end"])]


def hour_windows(rows, scope):
    hours = set()
    for row in rows:
        try:
            hours.add(
                int(datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING).timestamp()) // 3600 * 3600
            )
        except ValueError, TypeError:
            return None
    return [
        (max(scope["start"], h), min(scope["end"], h + 3599))
        for h in sorted(hours)
        if h <= scope["end"] and h + 3599 >= scope["start"]
    ]


def open_windows(store, shop, scope):
    """Known open creation dates plus one daily broad discovery scan."""
    from .order_plan import key
    from .incremental_collection import boundary
    from . import collection

    with store.connect() as db:
        meta = db.execute(
            "SELECT payload FROM business_cache WHERE shop_id=? AND cache_key=?", (shop["id"], key(shop))
        ).fetchone()
        if not meta or not json.loads(meta[0]).get("_order_rows_v2"):
            return None
        mark = db.execute(
            "SELECT value FROM settings WHERE key=?", (f"order-open-scan:{shop['id']}:{key(shop)}:{scope['status']}",)
        ).fetchone()
        if not mark or float(mark[0]) < boundary(collection.settings(store)):
            return None
        rows = db.execute(
            "SELECT created_at FROM order_entities WHERE shop_id=? AND cache_key=? AND payload IS NOT NULL AND (status=? OR json_extract(payload,'$._order_status_stale')=1) AND (coalesce(created_at,'')='' OR (created_at>=? AND created_at<=?))",
            (shop["id"], key(shop), scope["status"], scope["date_from"], scope["date_to"] + "\uffff"),
        ).fetchall()
    return hour_windows(rows, scope)
