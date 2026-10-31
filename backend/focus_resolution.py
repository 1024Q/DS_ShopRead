"""Durable resolution facts and coalesced follow-up reads of ordinary statuses."""

import json, time
from datetime import datetime, timedelta
from .business_reader import BEIJING


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS focus_resolutions(shop_id INTEGER,kind TEXT,id TEXT,
        payload TEXT NOT NULL,started REAL NOT NULL,pending INTEGER NOT NULL,requested REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(shop_id,kind,id))""")


def records(db, shop_id, kind):
    return {
        r["id"]: {**dict(r), "row": json.loads(r["payload"])}
        for r in db.execute("SELECT * FROM focus_resolutions WHERE shop_id=? AND kind=?", (shop_id, kind))
    }


def save(db, shop_id, kind, row, started, pending):
    old = db.execute(
        "SELECT started FROM focus_resolutions WHERE shop_id=? AND kind=? AND id=?", (shop_id, kind, str(row["id"]))
    ).fetchone()
    if old and old["started"] > started:
        return
    row = {**row, "attention_excluded": True, "focus_pending": bool(pending)}
    row.pop("attention_status", None)
    from .operations import decorate

    row = decorate(kind, row, "pdd")
    db.execute(
        """INSERT INTO focus_resolutions(shop_id,kind,id,payload,started,pending) VALUES(?,?,?,?,?,?)
        ON CONFLICT(shop_id,kind,id) DO UPDATE SET payload=excluded.payload,started=excluded.started,pending=excluded.pending""",
        (shop_id, kind, str(row["id"]), json.dumps(row, ensure_ascii=False), started, int(pending)),
    )
    from .focus_records import share

    share(db, shop_id, kind, [row], started)


def reopen(db, shop_id, kind, row_id):
    db.execute("DELETE FROM focus_resolutions WHERE shop_id=? AND kind=? AND id=?", (shop_id, kind, str(row_id)))


def scopes(store, shop_id, now=None):
    from .business import options_for

    now = time.time() if now is None else now
    with store.connect() as db:
        rows = db.execute(
            "SELECT kind,payload FROM focus_resolutions WHERE shop_id=? AND pending=1 AND (requested<started OR requested<?)",
            (shop_id, now - 300),
        ).fetchall()
    grouped = {}
    for r in rows:
        grouped.setdefault(r["kind"], []).append(json.loads(r["payload"]))
    for kind, items in grouped.items():
        opts = options_for(kind)
        days = [str(r.get("created_at") or "")[:10] for r in items]
        days = [d for d in days if len(d) == 10 and d[4:5] == "-" and d <= opts["date_to"]]
        if days:
            earliest = max(min(days), (datetime.fromtimestamp(now, BEIJING).date() - timedelta(days=180)).isoformat())
            opts = options_for(kind, date_from=min(opts["date_from"], earliest), date_to=opts["date_to"])
        yield kind, opts


def requested(store, shop_id, kind):
    with store.connect() as db:
        db.execute(
            "UPDATE focus_resolutions SET requested=? WHERE shop_id=? AND kind=? AND pending=1",
            (time.time(), shop_id, kind),
        )


def repair(store):
    """One-time repair of unresolved transitions recorded by earlier versions."""
    from .priority_rules import KINDS

    with store.connect() as db:
        if db.execute("SELECT 1 FROM settings WHERE key='focus_resolution_repair_v1'").fetchone():
            return
        latest = db.execute("""SELECT a.* FROM attention_changes a JOIN
            (SELECT shop_id,kind,record_id,max(id) id FROM attention_changes GROUP BY shop_id,kind,record_id) b ON a.id=b.id
            JOIN shops s ON s.id=a.shop_id AND s.platform='pdd' WHERE a.change='resolved' """).fetchall()
        changed = set()
        for event in latest:
            if event["kind"] not in KINDS:
                continue
            sid, kind, key = event["shop_id"], event["kind"], event["record_id"]
            if db.execute(
                "SELECT 1 FROM attention_rows WHERE shop_id=? AND kind=? AND id=?", (sid, kind, key)
            ).fetchone():
                continue
            old = db.execute(
                """SELECT r.payload,s.metadata FROM table_rows r JOIN table_snapshots s ON r.shop_id=s.shop_id AND r.cache_key=s.cache_key
                WHERE r.shop_id=? AND r.cache_key LIKE ? AND CAST(json_extract(r.payload,'$.id') AS TEXT)=?
                ORDER BY s.updated DESC LIMIT 1""",
                (sid, kind + ":%", key),
            ).fetchone()
            if not old:
                continue
            row = json.loads(old["payload"])
            observed = json.loads(old["metadata"]).get("_sync", {}).get("snapshot_started_at", 0)
            from .attention import wanted

            pending = observed < event["created"] or wanted(kind, {**row, "attention_excluded": False}, "pdd")
            save(db, sid, kind, row, max(event["created"], observed), pending)
            changed.add((sid, kind))
        for sid, kind in changed:
            store.invalidate_responses(sid, kind, db=db)
        db.execute("INSERT INTO settings(key,value) VALUES('focus_resolution_repair_v1','1')")
