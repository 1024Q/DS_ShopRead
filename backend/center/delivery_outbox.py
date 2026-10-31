"""Durable upload candidates, maintained in the same transaction as source writes."""

import json
from .replication import connect, record
from ..database_writer import connection

SOURCES = {
    "business_cache": ("business", "cache_key", "updated", "payload"),
    "daily_archive": ("auxiliary", "'archive:'||cache_key", "updated", "payload"),
    "home_history": ("auxiliary", "'history:'||kind||':'||day||':'||profile_version", "updated", "payload"),
    "attention_snapshots": ("auxiliary", "'attention:'||kind", "updated", "payload"),
    "tax_records": (
        "auxiliary",
        "'tax_record:'||period||':'||profile_version",
        "max(checked_at,baseline_at)",
        "json_object('baseline',json(baseline),'latest',json(latest),'baseline_at',baseline_at,'checked_at',checked_at)",
    ),
    "focus_resolutions": ("auxiliary", "'resolved:'||kind||':'||id", "started", "payload"),
}


def fair(candidates):
    """Round-robin shops so one shop's backlog never blocks every other shop.

    Order inside a shop stays oldest first; each batch takes one record per
    shop in turn, then the next record of each shop, and so on.
    """
    queues = {}
    for row in candidates:
        queues.setdefault(row[0], []).append(row)
    ordered = []
    while queues:
        for shop in list(queues):
            ordered.append(queues[shop].pop(0))
            if not queues[shop]:
                queues.pop(shop)
    return ordered


class Outbox:
    def __init__(self, path, lane, identity, legacy):
        self.path, self.lane = path, lane
        with connection(path, immediate=True) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS delivery_outbox(lane TEXT,shop_id INTEGER,scope TEXT,source TEXT,row_id INTEGER,updated REAL,ack REAL DEFAULT -1,confirmed REAL DEFAULT 0,attempts INTEGER DEFAULT 0,retry_at REAL DEFAULT 0,error TEXT DEFAULT '',rejected INTEGER DEFAULT 0,pending INTEGER DEFAULT 1,PRIMARY KEY(lane,shop_id,scope))"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS delivery_outbox_ready ON delivery_outbox(lane,pending,rejected,retry_at,updated)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS delivery_outbox_meta(key TEXT PRIMARY KEY,value TEXT)")
            columns = {r[1] for r in db.execute("PRAGMA table_info(delivery_outbox)")}
            for column, definition in (("content_hash", "TEXT DEFAULT ''"), ("content_updated", "REAL DEFAULT 0")):
                if column not in columns:
                    db.execute(f"ALTER TABLE delivery_outbox ADD COLUMN {column} {definition}")
            for table, (group, scope, stamp, _) in SOURCES.items():
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    continue
                # Initial scan and trigger installation are one transaction: no
                # source commit can fall between the baseline and change capture.
                marker = "source:" + table
                if db.execute("SELECT 1 FROM delivery_outbox_meta WHERE key=?", (marker,)).fetchone():
                    continue
                values = f"SELECT '{group}',shop_id,{scope},'{table}',rowid,{stamp} FROM {table}"
                insert = "INSERT INTO delivery_outbox(lane,shop_id,scope,source,row_id,updated) "
                update = (
                    " ON CONFLICT(lane,shop_id,scope) DO UPDATE SET row_id=excluded.row_id,updated=excluded.updated,"
                    + ",".join(
                        f"{column}=CASE WHEN excluded.updated!=delivery_outbox.updated THEN {value} ELSE {column} END"
                        for column, value in (
                            ("pending", "1"),
                            ("retry_at", "0"),
                            ("attempts", "0"),
                            ("error", "''"),
                            ("rejected", "0"),
                        )
                    )
                )
                db.execute(insert + values + " WHERE 1" + update)
                for action in ("INSERT", "UPDATE"):
                    db.execute(
                        f"CREATE TRIGGER delivery_capture_{table}_{action.lower()} AFTER {action} ON {table} BEGIN "
                        + insert
                        + values
                        + " WHERE rowid=NEW.rowid"
                        + update
                        + "; END"
                    )
                # REPLACE deletes and inserts in one source transaction; removing
                # an obsolete candidate is safe, its new insert recreates it.
                db.execute(
                    f"CREATE TRIGGER delivery_capture_{table}_delete AFTER DELETE ON {table} BEGIN DELETE FROM delivery_outbox WHERE source='{table}' AND row_id=OLD.rowid; END"
                )
                db.execute("INSERT INTO delivery_outbox_meta VALUES(?, '1')", (marker,))
            encoded = json.dumps(identity, sort_keys=True)
            key = "identity:" + lane
            previous = db.execute("SELECT value FROM delivery_outbox_meta WHERE key=?", (key,)).fetchone()
            if not previous or previous[0] != encoded:
                db.execute(
                    "UPDATE delivery_outbox SET ack=-1,confirmed=0,pending=1,rejected=0,retry_at=0,attempts=0,error='' WHERE lane=?",
                    (lane,),
                )
                if not previous and legacy.get("identity") == identity:
                    for ident, ack in legacy.get("acks", {}).items():
                        shop, scope = json.loads(ident)
                        db.execute(
                            "UPDATE delivery_outbox SET ack=?,confirmed=?,pending=(updated!=?) WHERE lane=? AND shop_id=? AND scope=?",
                            (ack, legacy.get("confirmed", {}).get(ident, 0), ack, lane, shop, scope),
                        )
                db.execute("INSERT OR REPLACE INTO delivery_outbox_meta VALUES(?,?)", (key, encoded))

    def rows(self, now, ignored):
        skipped = []
        with connect(self.path) as db:
            # Candidate identity, version and payload must belong to the same
            # WAL snapshot; a concurrent REPLACE can otherwise reuse a rowid.
            db.execute("BEGIN")
            candidates = fair(
                db.execute(
                    "SELECT shop_id,scope,updated,source,row_id FROM ("
                    "SELECT shop_id,scope,updated,source,row_id,retry_at,"
                    "ROW_NUMBER() OVER (PARTITION BY shop_id ORDER BY retry_at,updated,scope) AS turn "
                    "FROM delivery_outbox WHERE lane=? AND pending=1 AND rejected=0 AND retry_at<=?"
                    ") ORDER BY turn,retry_at,updated,shop_id LIMIT 200",
                    (self.lane, now),
                ).fetchall()
            )
            result = []
            size = 0
            for shop, scope, updated, table, rowid in candidates:
                if str(shop) in ignored:
                    skipped.append((self.lane, shop, scope, updated))
                    continue
                payload = SOURCES[table][3]
                row = db.execute(f"SELECT {payload} FROM {table} WHERE rowid=?", (rowid,)).fetchone()
                if not row:
                    skipped.append((self.lane, shop, scope, updated))
                    continue
                if table in ("home_history", "tax_records"):
                    if not db.execute(
                        f"SELECT 1 FROM {table} t JOIN shops s ON s.id=t.shop_id AND s.profile_version=t.profile_version WHERE t.rowid=?",
                        (rowid,),
                    ).fetchone():
                        skipped.append((self.lane, shop, scope, updated))
                        continue
                if table == "business_cache" and scope.startswith("orders:"):
                    value = json.loads(row[0])
                    if (
                        value.get("_sync", {}).get("scope", {}).get("_order_lane")
                        and db.execute(
                            "SELECT 1 FROM business_cache WHERE shop_id=? AND cache_key LIKE 'orders:view-v2:%' LIMIT 1",
                            (shop,),
                        ).fetchone()
                    ):
                        skipped.append((self.lane, shop, scope, updated))
                        continue
                length = len(row[0].encode("utf-8"))
                if result and size + length > 2 * 1024 * 1024:
                    break
                result.append(record(shop, scope, row[0], updated))
                size += length
                if len(result) >= 10:
                    break
        if skipped:
            with connection(self.path, immediate=True) as db:
                db.executemany(
                    "UPDATE delivery_outbox SET pending=0 WHERE lane=? AND shop_id=? AND scope=? AND updated=?", skipped
                )
        return result

    def state(self):
        with connect(self.path) as db:
            return db.execute(
                "SELECT shop_id,scope,ack,confirmed,attempts,retry_at,error,rejected,updated FROM delivery_outbox WHERE lane=? AND (ack>=0 OR attempts>0)",
                (self.lane,),
            ).fetchall()

    def counts(self):
        with connect(self.path) as db:
            rows = db.execute(
                "SELECT rejected,count(*),min(updated) FROM delivery_outbox WHERE lane=? AND pending=1 GROUP BY rejected",
                (self.lane,),
            ).fetchall()
        counts = {r[0]: r[1] for r in rows}
        self.oldest_pending = min((r[2] for r in rows if r[2] is not None), default=None)
        return counts.get(0, 0), counts.get(1, 0)

    def commit(self, rows, now):
        with connection(self.path, immediate=True) as db:
            db.executemany(
                "UPDATE delivery_outbox SET ack=?,confirmed=?,pending=(updated!=?),attempts=0,retry_at=0,error='',rejected=0 WHERE lane=? AND shop_id=? AND scope=?",
                [(r["updated"], now, r["updated"], self.lane, r["shop"], r["scope"]) for r in rows],
            )
            for r in rows:
                if r.get("_content_hash"):
                    db.execute(
                        "UPDATE delivery_outbox SET content_hash=?,content_updated=? WHERE lane=? AND shop_id=? AND scope=?",
                        (r["_content_hash"], r.get("_content_updated", r["updated"]), self.lane, r["shop"], r["scope"]),
                    )

    def content(self, shop, scope):
        with connect(self.path) as db:
            return db.execute(
                "SELECT content_hash,content_updated,ack FROM delivery_outbox WHERE lane=? AND shop_id=? AND scope=?",
                (self.lane, shop, scope),
            ).fetchone()

    def fail(self, rows, failures, rejected):
        with connection(self.path, immediate=True) as db:
            for row, failure in zip(rows, failures):
                db.execute(
                    "UPDATE delivery_outbox SET attempts=?,retry_at=?,error=?,rejected=? WHERE lane=? AND shop_id=? AND scope=? AND updated=?",
                    (
                        failure["attempts"],
                        failure["retry_at"],
                        failure["error"],
                        int(rejected and failure["attempts"] >= 3),
                        self.lane,
                        row["shop"],
                        row["scope"],
                        row["updated"],
                    ),
                )

    def reconcile(self, receipts, observed, content_receipts=None):
        with connection(self.path, immediate=True) as db:
            invalid_content = []
            if content_receipts is not None:
                for shop, scope, stamp in db.execute(
                    "SELECT shop_id,scope,content_updated FROM delivery_outbox WHERE lane=? AND content_hash!='' AND confirmed<?",
                    (self.lane, observed),
                ).fetchall():
                    key = json.dumps([str(shop), scope], ensure_ascii=False, separators=(",", ":"))
                    if content_receipts.get(key) != stamp:
                        invalid_content.append((self.lane, shop, scope))
                        db.execute(
                            "UPDATE delivery_outbox SET content_hash='' WHERE lane=? AND shop_id=? AND scope=?",
                            (self.lane, shop, scope),
                        )
            # Missing center receipts (for example after center restore) resend.
            db.execute(
                "UPDATE delivery_outbox SET ack=-1,pending=1 WHERE lane=? AND confirmed<?", (self.lane, observed)
            )
            for ident, stamp in receipts.items():
                shop, scope = json.loads(ident)
                db.execute(
                    "UPDATE delivery_outbox SET ack=?,confirmed=?,pending=(updated!=?) WHERE lane=? AND shop_id=? AND scope=? AND (confirmed<? OR ack<=?)",
                    (stamp, observed, stamp, self.lane, shop, scope, observed, stamp),
                )
            # A surviving heartbeat cannot confirm content lost in a partial
            # center restore. Repair immediately, even for a now-paused task.
            db.executemany(
                "UPDATE delivery_outbox SET ack=-1,pending=1,retry_at=0,rejected=0 WHERE lane=? AND shop_id=? AND scope=?",
                invalid_content,
            )
