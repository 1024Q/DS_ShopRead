"""Online backups, restore validation and daily retention (logs, run history, change history, old backups).
Business caches, financial and tax data are never deleted automatically."""

import asyncio, hashlib, json, re, sqlite3, time, uuid
from contextlib import closing
from datetime import datetime
from .business_reader import BEIJING

DEFAULTS = {
    "enabled": True,
    "time": "02:30",
    "keep": 7,
    "log_days": 30,
    "cache_days": 30,
    "run_days": 30,
    "change_days": 90,
}
PRUNE_INTERVAL = 86400
BACKUP_NAME = re.compile(r"backup-\d{8}-\d{6}-[0-9a-f]{6}")


def settings(store):
    with store.connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key='maintenance_policy'").fetchone()
    return {**DEFAULTS, **(json.loads(row[0]) if row else {})}


def save(store, value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("备份设置无效")
    result = {**settings(store), **value}
    if type(result["enabled"]) is not bool:
        raise ValueError("备份开关无效")
    if datetime.strptime(result["time"], "%H:%M").strftime("%H:%M") != result["time"]:
        raise ValueError("备份时间无效")
    for key, low, high in [
        ("keep", 2, 90),
        ("log_days", 7, 365),
        ("cache_days", 7, 365),
        ("run_days", 7, 365),
        ("change_days", 7, 3650),
    ]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError("保留期限超出范围")
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('maintenance_policy',?)", (json.dumps(result),))
    return result


def delete_before(store, table, column, cutoff, batch=5000):
    """Batched deletes keep each write transaction short; a missing table (older schema) is skipped."""
    if (table, column) not in (
        ("collection_runs", "finished"),
        ("attention_changes", "created"),
        ("event_logs", "last_seen"),
    ):
        raise ValueError("未允许的维护数据表")
    shards = getattr(store, "_shards", None)
    scopes = list(shards.paths) if shards and table in shards.tables else [None]
    total = 0
    # Retention is intentionally resumable per shop, never a cross-file transaction.
    for sid in scopes:
        while True:
            try:
                with store.node_db(write=True) if sid is None else store.shop_db(sid, write=True) as db:
                    count = db.execute(
                        f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {column}<? LIMIT ?)",
                        (cutoff, batch),
                    ).rowcount
            except sqlite3.OperationalError as error:
                if "no such table" in str(error):
                    break
                raise
            total += count
            if count < batch:
                break
    return total


def last_prune(store):
    with store.connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key='maintenance_last_prune'").fetchone()
    try:
        return json.loads(row[0]) if row else {}
    except ValueError:
        return {}


def database_capacity(directory):
    """Count live database families from the shard manifest, without opening them."""
    from pathlib import Path
    from .collector_update import read_json

    root = Path(directory).resolve()

    def size(path):
        try:
            return path.stat().st_size if path.is_file() else 0
        except FileNotFoundError:
            return 0  # WAL may be checkpointed during listing.

    def family(path):
        return sum(size(Path(str(path) + suffix)) for suffix in ("", "-wal", "-shm"))

    management = root / "shops.sqlite3"
    paths = set()
    for entry in read_json(root / "shop-storage.json", {}).get("shops", {}).values():
        if entry.get("state") != "active" or not entry.get("path"):
            continue
        path = (root / entry["path"]).resolve()
        if path.is_relative_to(root / "shops") and path.name == "business.sqlite3" and path.is_file():
            paths.add(path)
    management_bytes = family(management)
    business_bytes = sum(family(path) for path in paths)
    return {
        "database_bytes": management_bytes + business_bytes,
        "management_bytes": management_bytes,
        "business_bytes": business_bytes,
        "wal_bytes": sum(size(Path(str(path) + "-wal")) for path in [management, *paths]),
        "shop_database_count": len(paths),
    }


class Maintenance:
    def __init__(self, store):
        self.store = store
        self.lock = asyncio.Lock()
        self.last_error = ""

    def listing(self):
        import shutil

        disk = shutil.disk_usage(self.store.directory)
        capacity = database_capacity(self.store.directory)
        backup_bytes = sum(
            p.stat().st_size for p in (self.store.directory / "backups").glob("*.sqlite3") if p.is_file()
        )
        items = []
        for path in sorted((self.store.directory / "backups").glob("backup-*.json"), reverse=True):
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except OSError, ValueError:
                continue
        policy = settings(self.store)
        with self.store.connect() as db:
            logs = db.execute(
                "SELECT COUNT(*) FROM event_logs WHERE last_seen<?", (time.time() - policy["log_days"] * 86400,)
            ).fetchone()[0]
            caches = db.execute(
                "SELECT COUNT(*) FROM business_cache WHERE updated<? AND cache_key NOT LIKE 'tax:%' AND cache_key NOT LIKE 'reconciliation_%'",
                (time.time() - policy["cache_days"] * 86400,),
            ).fetchone()[0]
        return {
            "settings": policy,
            "backups": items,
            "running": self.lock.locked(),
            "error": self.last_error,
            "capacity": {
                **capacity,
                "backup_bytes": backup_bytes,
                "free_bytes": disk.free,
                "total_bytes": disk.total,
                "warning": disk.free < max(5 * 1024**3, disk.total * 0.1),
            },
            "retention_preview": {
                "logs": logs,
                "caches": caches,
                "backups": max(0, len(items) - policy["keep"]),
                "automatic_deletion": True,
                "automatic": {
                    "log_days": policy["log_days"],
                    "run_days": policy["run_days"],
                    "change_days": policy["change_days"],
                    "backups_keep": policy["keep"],
                    "caches": False,
                },
                "last_prune": last_prune(self.store),
            },
            "key_required": "恢复加密账号需要原 credentials.key，请另行妥善保存；备份不含明文密码。",
        }

    def create(self):
        folder = self.store.directory / "backups"
        folder.mkdir(exist_ok=True)
        name = "backup-" + datetime.now(BEIJING).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        path = folder / (name + ".sqlite3")
        with closing(sqlite3.connect(self.store.path, timeout=30)) as source, closing(sqlite3.connect(path)) as target:
            source.execute("BEGIN")
            source.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            source.backup(target, pages=256, sleep=0.02)
        if self.store._shards:
            from .shop_storage import merge_snapshot

            merge_snapshot(self.store.directory, path)
        # Restore into a temporary file, never replace the live database.
        import tempfile

        with tempfile.TemporaryDirectory(prefix="pdd-restore-", dir=folder) as temp:
            from pathlib import Path

            with (
                closing(sqlite3.connect(path)) as source,
                closing(sqlite3.connect(Path(temp) / "restore.sqlite3")) as restored,
            ):
                source.backup(restored)
                if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("备份完整性校验失败")
                count = restored.execute("SELECT COUNT(*) FROM shops").fetchone()[0]
                for row in restored.execute("SELECT password FROM shops"):
                    self.store.cipher.decrypt(row[0])
                for table in ("tax_records", "daily_archive", "collection_tasks"):
                    restored.execute("SELECT COUNT(*) FROM " + table).fetchone()
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        info = {
            "name": name,
            "created_at": datetime.now(BEIJING).isoformat(),
            "size": path.stat().st_size,
            "shops": count,
            "verified": True,
            "sha256": digest.hexdigest(),
        }
        (folder / (name + ".json")).write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        return info

    def prune_backups(self, keep):
        """Keep the newest `keep` backups and always the newest verified one; only removes files this service created."""
        folder = self.store.directory / "backups"
        if not folder.is_dir():
            return 0
        items = []
        for path in sorted(folder.glob("backup-*.json"), reverse=True):
            if not BACKUP_NAME.fullmatch(path.stem):
                continue
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
            except OSError, ValueError:
                info = {}
            items.append((path.stem, bool(info.get("verified"))))
        retained = {name for name, _ in items[: max(1, keep)]}
        verified = next((name for name, ok in items if ok), None)
        if verified:
            retained.add(verified)
        removed = 0
        for name, _ in items:
            if name in retained:
                continue
            for suffix in (".sqlite3", ".json"):
                (folder / (name + suffix)).unlink(missing_ok=True)
            removed += 1
        # A failed backup leaves a database file without its verified description.
        known = {name for name, _ in items}
        for path in folder.glob("backup-*.sqlite3"):
            if (
                BACKUP_NAME.fullmatch(path.stem)
                and path.stem not in known
                and time.time() - path.stat().st_mtime > 86400
            ):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def prune(self):
        from .log_spool import get as log_files

        policy = settings(self.store)
        now = time.time()
        result = {
            "at": now,
            "logs": log_files(self.store).flush(now=now),
            "runs": delete_before(self.store, "collection_runs", "finished", now - policy["run_days"] * 86400),
            "changes": delete_before(self.store, "attention_changes", "created", now - policy["change_days"] * 86400),
            "backups": self.prune_backups(policy["keep"]),
        }
        with self.store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES('maintenance_last_prune',?)", (json.dumps(result),)
            )
        return result

    async def prune_due(self):
        if (
            self.lock.locked()
            or time.time() - float((await asyncio.to_thread(last_prune, self.store)).get("at", 0)) < PRUNE_INTERVAL
        ):
            return None
        async with self.lock:
            result = await asyncio.to_thread(self.prune)
        if any(result[k] for k in ("logs", "runs", "changes", "backups")):
            await asyncio.to_thread(
                self.store.log_event,
                "info",
                "system",
                "retention_cleanup",
                f"自动清理完成：日志 {result['logs']} 条，采集记录 {result['runs']} 条，变化记录 {result['changes']} 条，旧备份 {result['backups']} 份",
            )
        return result

    async def backup(self):
        if self.lock.locked():
            raise ValueError("备份正在进行")
        async with self.lock:
            try:
                result = await asyncio.to_thread(self.create)
                self.last_error = ""
                await asyncio.to_thread(
                    self.store.log_event, "info", "system", "backup_verified", "数据库备份及临时恢复验证完成"
                )
                try:
                    await asyncio.to_thread(self.prune_backups, (await asyncio.to_thread(settings, self.store))["keep"])
                except OSError:
                    pass
                return result
            except Exception:
                self.last_error = "数据库备份或恢复验证失败，请检查磁盘空间和文件权限"
                await asyncio.to_thread(self.store.log_event, "error", "system", "backup_failed", self.last_error)
                raise

    async def run(self):
        while True:
            try:
                policy = await asyncio.to_thread(settings, self.store)
                now = datetime.now(BEIJING)
                items = (await asyncio.to_thread(self.listing))["backups"]
                if (
                    policy["enabled"]
                    and now.strftime("%H:%M") >= policy["time"]
                    and not any(x["created_at"][:10] == now.date().isoformat() for x in items)
                ):
                    await self.backup()
                await self.prune_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_error = "数据库维护暂未完成，将在下个周期重试"
                await asyncio.sleep(30)
            await asyncio.sleep(30)

    def verify_file(self, name):
        import re, tempfile
        from pathlib import Path

        if not re.fullmatch(r"backup-\d{8}-\d{6}-[0-9a-f]{6}", name):
            raise ValueError("备份名称无效")
        folder = (self.store.directory / "backups").resolve()
        path = folder / (name + ".sqlite3")
        if not path.is_file() or path.resolve().parent != folder:
            raise ValueError("备份文件不存在")
        info = json.loads((folder / (name + ".json")).read_text(encoding="utf-8"))
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != info.get("sha256"):
            raise ValueError("备份指纹不一致，文件可能已损坏；未更改当前数据库")
        with tempfile.TemporaryDirectory(prefix="verify-", dir=folder) as tmp:
            with (
                closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source,
                closing(sqlite3.connect(Path(tmp) / "preview.sqlite3")) as target,
            ):
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("备份数据库完整性校验失败")
                counts = {
                    t: target.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
                    for t in ("shops", "tax_records", "daily_archive", "collection_tasks")
                }
                for row in target.execute("SELECT password FROM shops"):
                    self.store.cipher.decrypt(row[0])
        return {
            "name": name,
            "verified": True,
            "counts": counts,
            "live_database_unchanged": True,
            "verified_at": datetime.now(BEIJING).isoformat(),
        }

    async def verify(self, name):
        if self.lock.locked():
            raise ValueError("备份任务正在进行")
        async with self.lock:
            return await asyncio.to_thread(self.verify_file, name)
