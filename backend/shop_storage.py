"""Versioned per-shop storage migration. The legacy database remains the rollback source."""

import hashlib, sqlite3, time, uuid, threading, shutil
from pathlib import Path
from contextlib import closing
from .database_writer import connection
from .collector_update import write_json, read_json

MANAGEMENT = {
    "shops",
    "operators",
    "shop_operators",
    "window_preferences",
    "debug_modes",
    "event_logs",
    "invoice_import_batches",
    "invoice_import_files",
    "invoice_upload_claims",
    "remote_nodes",
    "fleet_mirrors",
    "settings",
    "collector_runtime",
    "log_spool_cursors",
}


def metadata(path):
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        columns = {n: [r[1] for r in db.execute('PRAGMA table_info("' + n + '")')] for n, _ in rows}
        tables = {n: sql for n, sql in rows if n not in MANAGEMENT and ("shop_id" in columns[n] or n == "order_clock")}
        extras = [
            (n, t, sql)
            for n, t, sql in db.execute(
                "SELECT name,tbl_name,sql FROM sqlite_master WHERE type IN ('index','trigger') AND sql IS NOT NULL"
            )
            if t in tables
        ]
    return tables, columns, extras


def digest(db, table, sid=None, heartbeat=None):
    where = " WHERE shop_id=?" if sid is not None else ""
    result = hashlib.sha256()
    count = 0
    # Physical insertion order is preserved by migration; rowid is stable for these tables.
    for row in db.execute('SELECT * FROM "' + table + '"' + where + " ORDER BY rowid", () if sid is None else (sid,)):
        result.update(repr(tuple(row)).encode("utf-8"))
        result.update(b"\n")
        count += 1
        if heartbeat and count % 500 == 0:
            heartbeat(table)
    return {"rows": count, "sha256": result.hexdigest()}


class ShopStorage:
    def __init__(self, store):
        self.lock = threading.RLock()
        self.store = store
        self.root = store.directory
        self.manifest = self.root / "shop-storage.json"
        self.tables, self.columns, self.extras = metadata(store.path)
        self.state = read_json(self.manifest, {"version": 1, "shops": {}})
        self.paths = {
            int(k): self.root / v["path"] for k, v in self.state["shops"].items() if v.get("state") == "active"
        }

    def migrate(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            ids = [r[0] for r in db.execute("SELECT id FROM shops")]
        missing = [sid for sid in ids if sid not in self.paths]
        if missing and shutil.disk_usage(self.root).free < max(
            512 * 1024**2, int(self.store.path.stat().st_size * 1.25)
        ):
            raise RuntimeError("店铺数据库迁移空间不足，原数据库已保留；请释放磁盘空间后重试")
        for sid in ids:
            if sid in self.paths:
                if not self.paths[sid].is_file():
                    raise RuntimeError(f"店铺 {sid} 的业务数据库缺失，已停止启动以避免使用旧数据")
                self.repair_refund_index(sid)
                from .schema_migrations import shop as migrate_schema

                with connection(self.paths[sid], immediate=True) as dest:
                    migrate_schema(dest)
                continue
            self.migrate_shop(sid)

    def repair_refund_index(self, sid):
        # Older releases created this table lazily in the node database, after
        # shards had already been migrated. Upgrade only the missing table.
        with connection(self.paths[sid], immediate=True) as dest:
            if dest.execute("SELECT 1 FROM sqlite_master WHERE name='refund_case_index'").fetchone():
                return
            from .refund_index import initialize

            initialize(dest)
            with closing(sqlite3.connect(self.store.path)) as source:
                for row in source.execute(
                    "SELECT shop_id,case_id,order_number,amount,updated FROM refund_case_index WHERE shop_id=?", (sid,)
                ):
                    dest.execute("INSERT INTO refund_case_index VALUES(?,?,?,?,?)", row)

    def migrate_shop(self, sid):
        with self.lock:
            if sid in self.paths:
                return
            self._migrate_shop(sid)

    def _migrate_shop(self, sid):
        folder = self.root / "shops" / str(int(sid))
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "business.sqlite3"
        stage = folder / "migration.sqlite3"
        # Startup is quiescent: collection and delivery are not admitted before migration completes.
        write_json(
            self.root / "storage-migration-status.json", {"shop": sid, "state": "migrating", "updated": time.time()}
        )
        stage.unlink(missing_ok=True)
        checks = {}
        last_heartbeat = [0]

        def heartbeat(table):
            if time.monotonic() - last_heartbeat[0] < 5:
                return
            last_heartbeat[0] = time.monotonic()
            write_json(
                self.root / "storage-migration-status.json",
                {"shop": sid, "table": table, "state": "migrating", "updated": time.time()},
            )

        with closing(sqlite3.connect(self.store.path)) as source, closing(sqlite3.connect(stage)) as dest:
            source.execute("BEGIN")
            for table, sql in self.tables.items():
                write_json(
                    self.root / "storage-migration-status.json",
                    {"shop": sid, "table": table, "state": "migrating", "updated": time.time()},
                )
                dest.execute(sql)
                if table == "order_clock":
                    row = source.execute("SELECT value FROM order_clock WHERE id=1").fetchone()
                    dest.execute("INSERT INTO order_clock VALUES(1,?,?)", (uuid.uuid4().hex, row[0] if row else 0))
                    continue
                cursor = source.execute('SELECT * FROM "' + table + '" WHERE shop_id=? ORDER BY rowid', (sid,))
                while batch := cursor.fetchmany(500):
                    dest.executemany(
                        'INSERT INTO "' + table + '" VALUES(' + ",".join("?" for _ in batch[0]) + ")", batch
                    )
                    heartbeat(table)
                checks[table] = digest(source, table, sid, heartbeat)
                if digest(dest, table, sid, heartbeat) != checks[table]:
                    raise RuntimeError(f"店铺 {sid} 的 {table} 迁移校验不一致")
            dest.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            for row in source.execute("SELECT key,value FROM settings WHERE key LIKE ?", (f"order-open-scan:{sid}:%",)):
                dest.execute("INSERT INTO settings VALUES(?,?)", row)
            for _, _, sql in self.extras:
                dest.execute(sql)
            for table, sql in self.tables.items():
                if "AUTOINCREMENT" in sql.upper():
                    if not dest.execute(
                        "UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?", (int(sid) << 40, table)
                    ).rowcount:
                        dest.execute("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", (table, int(sid) << 40))
            stream = dest.execute("SELECT stream FROM order_clock WHERE id=1").fetchone()[0]
            dest.execute(
                "UPDATE business_cache SET payload=json_set(payload,'$._order_stream',?),updated=? WHERE cache_key LIKE 'orders:view-v2:%'",
                (stream, time.time()),
            )
            from .schema_migrations import shop as migrate_schema

            migrate_schema(dest)
            dest.commit()
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError(f"店铺 {sid} 数据库完整性校验失败")
        stage.replace(target)
        with closing(sqlite3.connect(target)) as db:
            db.execute("PRAGMA journal_mode=WAL")
        # Preserve only this shop's snapshot acknowledgements; orders use a new stream.
        for lane in ("business", "auxiliary"):
            receipt = read_json(self.root / f"center-delivery-{lane}.json", {})
            for field in ("acks", "confirmed", "retries", "inflight"):
                import json

                receipt[field] = {k: v for k, v in receipt.get(field, {}).items() if str(json.loads(k)[0]) == str(sid)}
            write_json(folder / f"center-delivery-{lane}.json", receipt)
        self.state["shops"][str(sid)] = {
            "path": target.relative_to(self.root).as_posix(),
            "state": "active",
            "migrated_at": time.time(),
            "checks": checks,
        }
        write_json(self.manifest, self.state)
        self.paths[sid] = target
        write_json(self.root / "storage-migration-status.json", {"shop": sid, "state": "ready", "updated": time.time()})

    def path(self, sid):
        sid = int(sid)
        if sid not in self.paths:
            self.migrate_shop(sid)
        return self.paths[sid]


def merge_snapshot(directory, target):
    """Build a legacy-readable copy. Never modifies any live database."""
    root = Path(directory)
    target = Path(target)
    state = read_json(root / "shop-storage.json", {})
    legacy = root / "shops.sqlite3"
    tables, columns, extras = metadata(legacy)
    with closing(sqlite3.connect(legacy)) as source, closing(sqlite3.connect(target)) as db:
        source.backup(db)
        triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall()
        for name, _ in triggers:
            db.execute('DROP TRIGGER "' + name + '"')
        for sid, entry in state.get("shops", {}).items():
            if entry.get("state") != "active":
                continue
            with closing(sqlite3.connect(root / entry["path"])) as shard:
                shard.execute("BEGIN")
                order_ordinals = {}
                for table in sorted(tables, key=lambda t: 0 if t == "order_entities" else 1):
                    if table == "order_clock":
                        continue
                    db.execute('DELETE FROM "' + table + '" WHERE shop_id=?', (int(sid),))
                    cols = columns[table]
                    if table == "collection_runs" or ("AUTOINCREMENT" in tables[table].upper() and "id" in cols):
                        cols = [c for c in cols if c != "id"]
                    names = ",".join('"' + c + '"' for c in cols)
                    cursor = shard.execute("SELECT " + names + ' FROM "' + table + '" WHERE shop_id=?', (int(sid),))
                    count = 0
                    while rows := cursor.fetchmany(500):
                        if table == "order_entities":
                            destcols = [c for c in cols if c != "ordinal"]
                            for row in rows:
                                values = dict(zip(cols, row))
                                old = values.pop("ordinal")
                                inserted = db.execute(
                                    "INSERT INTO order_entities ("
                                    + ",".join(destcols)
                                    + ") VALUES("
                                    + ",".join("?" for _ in destcols)
                                    + ")",
                                    [values[c] for c in destcols],
                                )
                                order_ordinals[(values["cache_key"], old)] = inserted.lastrowid
                        else:
                            if table == "table_rows":
                                index = cols.index("ordinal")
                                keyindex = cols.index("cache_key")
                                adjusted = []
                                for row in rows:
                                    row = list(row)
                                    row[index] = order_ordinals.get((row[keyindex], row[index]), row[index])
                                    adjusted.append(row)
                                rows = adjusted
                            db.executemany(
                                'INSERT INTO "'
                                + table
                                + '" ('
                                + names
                                + ") VALUES("
                                + ",".join("?" for _ in cols)
                                + ")",
                                rows,
                            )
                        count += len(rows)
                    if (
                        db.execute('SELECT count(*) FROM "' + table + '" WHERE shop_id=?', (int(sid),)).fetchone()[0]
                        != count
                    ):
                        raise ValueError("回退行数校验失败")
                for row in shard.execute("SELECT key,value FROM settings"):
                    db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", row)
        stream = uuid.uuid4().hex
        db.execute("UPDATE order_entities SET revision=ordinal")
        db.execute(
            "UPDATE order_clock SET stream=?,value=(SELECT coalesce(max(revision),0) FROM order_entities) WHERE id=1",
            (stream,),
        )
        db.execute(
            "UPDATE business_cache SET payload=json_set(payload,'$._order_stream',?),updated=? WHERE cache_key LIKE 'orders:view-v2:%'",
            (stream, time.time()),
        )
        for _, sql in triggers:
            db.execute(sql)
        db.commit()
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("回退完整性校验失败")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA journal_mode=DELETE")


def _rollback(directory):
    """Services must be stopped. Failed merging leaves both live formats untouched."""
    root = Path(directory)
    manifest = root / "shop-storage.json"
    state = read_json(manifest, {})
    if not any(e.get("state") == "active" for e in state.get("shops", {}).values()):
        return
    legacy = root / "shops.sqlite3"
    stage = root / "storage-rollback-stage.sqlite3"
    stage.unlink(missing_ok=True)
    merge_snapshot(root, stage)
    with (
        closing(sqlite3.connect(legacy)) as source,
        closing(sqlite3.connect(root / "storage-rollback-backup.sqlite3")) as saved,
    ):
        source.backup(saved)
        source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # The collector and delivery processes are stopped; no live connection survives.
    for suffix in ("-wal", "-shm"):
        Path(str(legacy) + suffix).unlink(missing_ok=True)
    stage.replace(legacy)
    state["rolled_back_at"] = time.time()
    for entry in state["shops"].values():
        entry["state"] = "rolled_back"
    write_json(manifest, state)


def rollback(directory):
    from .collector_lease import exclusive

    root = Path(directory)
    if not (root / "shop-storage.json").exists():
        return
    with exclusive(root):
        _rollback(root)
