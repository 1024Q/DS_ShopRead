import os
import re
import sqlite3
import json
import threading
import time
from pathlib import Path
from contextlib import contextmanager
from cryptography.fernet import Fernet
from .audit_store import AuditStore

ROOT = Path(__file__).resolve().parents[1]
# Ordinary UI snapshots kept per shop beyond live scheduled scopes and financial records.
CACHE_PRUNE_SECONDS = 0.6
CACHE_LIMIT = 48


class Store(AuditStore):
    def __init__(self, directory=None):
        self._shards = None
        self._response_lock = threading.Lock()
        self._cache_prune_lock = threading.Lock()
        self._cache_pruned = {}
        from .database_writer import queue_for

        self.directory = Path(directory or os.getenv("PDD_DATA_DIR") or ROOT / ".data")
        self.directory.mkdir(parents=True, exist_ok=True)
        from .credentials_key import load as load_key

        self.cipher = Fernet(load_key(self.directory))
        self.path = self.directory / "shops.sqlite3"
        self._write_lock = queue_for(self.path)
        # Readers keep their committed snapshot while collection publishes rows.
        from .database_writer import connection

        with connection(self.path, timeout=30) as setup:
            setup.execute("PRAGMA journal_mode=WAL")
        with self.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS shops (
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT '',
              username TEXT NOT NULL COLLATE NOCASE UNIQUE, password BLOB NOT NULL,
              browser TEXT NOT NULL DEFAULT 'system');
              CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);""")
            from .schema_migrations import node as migrate_node

            migrate_node(db)
            db.execute("CREATE TABLE IF NOT EXISTS operators (name TEXT PRIMARY KEY COLLATE NOCASE)")
            from .operators import initialize as initialize_operators

            initialize_operators(db)
            db.execute(
                "CREATE TABLE IF NOT EXISTS business_cache (shop_id INTEGER, cache_key TEXT, payload TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(shop_id,cache_key))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS window_preferences (shop_id INTEGER PRIMARY KEY, visible INTEGER NOT NULL)"
            )
            # Management table keyed by "shop" (not "shop_id"), so neither this nor an older
            # code version treats it as a per-shop business table: rollback-safe.
            db.execute("CREATE TABLE IF NOT EXISTS debug_modes (shop INTEGER PRIMARY KEY, enabled INTEGER NOT NULL)")
        from .collector_update import read_json

        manifest = read_json(self.directory / "shop-storage.json", {})
        # With per-shop databases active, business tables in the node database are a
        # frozen rollback copy: skip re-indexing it on every start (was ~30 s).
        self.sharded = os.environ.get("ECMP_SHOP_DATABASES") == "1" or any(
            e.get("state") == "active" for e in manifest.get("shops", {}).values()
        )
        timings = []

        def step(name, function, *args):
            started = time.monotonic()
            result = function(*args)
            timings.append((name, time.monotonic() - started))
            return result

        from .data_versions import initialize as initialize_versions

        with self.connect() as db:
            step("data_versions", initialize_versions, db)
        from .runtime_state import initialize as initialize_runtime

        step("runtime_state", initialize_runtime, self)
        from .collection import initialize as initialize_collection

        step("collection", initialize_collection, self)
        from .operations_insights import initialize as initialize_insights

        step("operations_insights", initialize_insights, self)
        from .attention import initialize as initialize_attention

        step("attention", initialize_attention, self)
        step("audit", self.initialize_audit)
        from .tax_review import initialize

        step("tax_review", initialize, self)
        from .order_storage import initialize as initialize_orders

        step("order_storage", initialize_orders, self)
        from .table_index import initialize as initialize_tables

        step("table_index", initialize_tables, self, not self.sharded)
        from .focus_resolution import repair

        step("focus_resolution", repair, self)
        from .payment_daily import initialize as initialize_payment

        step("payment_daily", initialize_payment, self)
        from .invoice_import_store import initialize as initialize_imports

        step("invoice_imports", initialize_imports, self)
        from .refund_index import initialize as initialize_refunds

        with self.connect() as db:
            step("refund_index", initialize_refunds, db)
        if self.sharded:
            step("shop_databases", self.enable_shop_databases)
        self.startup_timings = timings
        slow = [(name, seconds) for name, seconds in timings if seconds >= 1]
        if slow:
            import logging

            logging.getLogger(__name__).warning(
                "数据库启动初始化较慢：%s", "，".join(f"{name} {seconds:.1f} 秒" for name, seconds in slow)
            )

    def enable_shop_databases(self):
        from .shop_storage import ShopStorage

        manager = ShopStorage(self)
        with self.connect() as db:
            ids = {r[0] for r in db.execute("SELECT id FROM shops")}
        if ids - set(manager.paths):
            from .collector_lease import exclusive

            with exclusive(self.directory):
                manager.migrate()
        else:
            manager.migrate()
        self._shards = manager

    def writer_for(self, shop_id):
        if not self._shards:
            return self._write_lock
        from .database_writer import queue_for

        return queue_for(self._shards.path(shop_id))

    def write_status(self):
        base = self._write_lock.status()
        if not self._shards:
            return base
        shops = [{"shop_id": sid, **self.writer_for(sid).status()} for sid in list(self._shards.paths)]
        states = [base, *shops]
        result = {**base, "per_shop": shops, "sharded": True, "shop_count": len(shops)}
        for key in ("pending", "timeouts", "completed"):
            result[key] = sum(v[key] for v in states)
        for key in ("oldest_seconds", "active_seconds", "last_ms"):
            result[key] = max(v[key] for v in states)
        for key in ("pressure", "active", "external_wait"):
            result[key] = any(v[key] for v in states)
        result["pressured_shops"] = sum(v["pressure"] for v in shops)
        return result

    @contextmanager
    def node_db(self, *, write=False, timeout=60):
        """Explicit node metadata connection; no SQL shard inference."""
        from .database_writer import connection

        with connection(self.path, timeout, immediate=write, read_only=not write) as db:
            yield db

    @contextmanager
    def shop_db(self, shop_id, *, write=False, timeout=60):
        """Explicit single-shop database. Callers must still bind shop_id in SQL."""
        if type(shop_id) is not int or shop_id <= 0:
            raise ValueError("店铺数据库需要有效店铺编号")
        from .database_writer import connection

        path = self._shards.path(shop_id) if self._shards else self.path
        with connection(path, timeout, immediate=write, read_only=not write) as db:
            yield db

    @contextmanager
    def connect(self):
        if self._shards:
            from .shard_connection import FederatedConnection

            with FederatedConnection(self) as db:
                yield db
        else:
            from .database_writer import connection

            with connection(self.path) as db:
                yield db

    @contextmanager
    def write_connection(self, timeout=60):
        if self._shards:
            from .shard_connection import FederatedConnection

            with FederatedConnection(self, immediate=True, timeout=timeout) as db:
                yield db
        else:
            from .database_writer import connection

            with connection(self.path, timeout, immediate=True) as db:
                yield db

    def list(self):
        with self.connect() as db:
            from .operators import attach

            return attach(
                db,
                [
                    {**dict(r), "debug_mode": bool(r["debug_mode"])}
                    for r in db.execute(
                        "SELECT id,CASE WHEN alias<>'' THEN alias ELSE name END AS name,name AS detected_name,alias,username,browser,profile_version,platform,operator,login_method,login_url,"
                        "COALESCE((SELECT enabled FROM debug_modes d WHERE d.shop=shops.id),0) AS debug_mode FROM shops ORDER BY id"
                    )
                ],
            )

    def operators(self):
        with self.connect() as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT name FROM operators UNION SELECT operator FROM shops WHERE operator<>'' ORDER BY 1"
                )
            ]

    def save_operators(self, values):
        from .operators import names

        people = names(values)
        if not people:
            raise ValueError("请填写运营人员姓名")
        with self.connect() as db:
            db.executemany("INSERT OR IGNORE INTO operators(name) VALUES(?)", [(n,) for n in people])
            from .data_versions import publish

            publish(db)

    def save_operator(self, name, previous=None):
        from .operators import names, replace, publish

        people = names([name])
        if not people:
            raise ValueError("请填写有效的运营人员姓名")
        name = people[0]
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO operators(name) VALUES(?)", (name,))
            publish(db, [0])
            if previous and previous != name:
                ids = [r[0] for r in db.execute("SELECT shop_id FROM shop_operators WHERE name=?", (previous,))]
                for sid in ids:
                    assigned = [
                        name if r[0].casefold() == previous.casefold() else r[0]
                        for r in db.execute("SELECT name FROM shop_operators WHERE shop_id=?", (sid,))
                    ]
                    replace(db, sid, names(assigned))
                if previous.casefold() != name.casefold():
                    db.execute("DELETE FROM operators WHERE name=?", (previous,))
                else:
                    db.execute("UPDATE operators SET name=? WHERE name=?", (name, previous))
                publish(db, ids)

    def assign_operator(self, ids, name, mode="replace"):
        from .operators import names, replace, publish

        people = names(name)
        if mode not in ("replace", "append", "remove"):
            raise ValueError("分配方式无效")
        ids = set(ids)
        if not ids:
            raise ValueError("请先选择店铺")
        with self.connect() as db:
            existing = {r[0] for r in db.execute("SELECT id FROM shops")}
            if not ids <= existing:
                raise ValueError("部分店铺不存在，请刷新后重试")
            for sid in ids:
                old = [r[0] for r in db.execute("SELECT name FROM shop_operators WHERE shop_id=?", (sid,))]
                value = (
                    names(old + people)
                    if mode == "append"
                    else [n for n in old if n.casefold() not in {p.casefold() for p in people}]
                    if mode == "remove"
                    else people
                )
                replace(db, sid, value)
            publish(db, ids)

    def window_preference(self, shop_id):
        with self.connect() as db:
            row = db.execute("SELECT visible FROM window_preferences WHERE shop_id=?", (shop_id,)).fetchone()
        return bool(row["visible"]) if row else None

    def debug_mode(self, shop_id):
        """Per-shop debug mode: show the browser during collection. Default off (silent)."""
        with self.connect() as db:
            row = db.execute("SELECT enabled FROM debug_modes WHERE shop=?", (shop_id,)).fetchone()
        return bool(row["enabled"]) if row else False

    def set_debug_mode(self, shop_id, enabled):
        if type(enabled) is not bool:
            raise ValueError("调试模式设置无效")
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM shops WHERE id=?", (shop_id,)).fetchone():
                raise ValueError("店铺不存在")
            db.execute("INSERT OR REPLACE INTO debug_modes(shop,enabled) VALUES(?,?)", (shop_id, int(enabled)))

    def set_window_preference(self, shop_id, visible):
        if type(visible) is not bool:
            raise ValueError("窗口显示设置无效")
        with self.connect() as db:
            if db.execute("SELECT 1 FROM shops WHERE id=?", (shop_id,)).fetchone():
                db.execute(
                    "INSERT OR REPLACE INTO window_preferences(shop_id,visible) VALUES(?,?)", (shop_id, int(visible))
                )

    def add(
        self,
        username,
        password,
        browser="system",
        name="",
        platform="pdd",
        operator="",
        login_method="password",
        login_url="",
    ):
        if login_method == "qr" and not username.strip():
            import uuid

            username = "qr-" + uuid.uuid4().hex
        self.validate(username, password, browser, name, platform, operator, login_method, login_url)
        with self.connect() as db:
            shop_id = db.execute(
                "INSERT INTO shops(alias,username,password,browser,platform,operator,login_method,login_url) VALUES(?,?,?,?,?,?,?,?)",
                (
                    name.strip(),
                    username.strip(),
                    self.cipher.encrypt(password.encode()),
                    browser,
                    platform,
                    "",
                    login_method,
                    login_url,
                ),
            ).lastrowid
            from .operators import names, replace

            replace(db, shop_id, names(operator))
            return shop_id

    @staticmethod
    def validate(username, password, browser, name, platform="pdd", operator="", login_method="password", login_url=""):
        from .platforms import validate_platform

        validate_platform(platform)
        from .platform_login import validate_url

        validate_url(login_url)
        if login_method == "qr" and platform == "other" and not login_url:
            raise ValueError("其他平台扫码登录需要填写官方登录地址")
        if login_method not in ("password", "qr"):
            raise ValueError("该平台暂不支持此登录方式")
        from .operators import names

        names(operator)
        if (
            browser not in ("system", "bundled")
            or not username.strip()
            or (not password and login_method == "password")
            or any(c in username + password + name for c in "\r\n\0")
        ):
            raise ValueError("账号、密码或浏览器配置不正确")
        if len(username) > 256 or len(password) > 1024 or len(name) > 100:
            raise ValueError("配置内容过长")

    def update(
        self,
        shop_id,
        username,
        password="",
        browser="system",
        name="",
        platform=None,
        operator=None,
        login_method=None,
        login_url=None,
    ):
        old = self.credentials(shop_id)
        password = password or old["password"]
        platform = platform or old.get("platform", "pdd")
        if operator is None:
            with self.connect() as db:
                operator = [
                    r[0]
                    for r in db.execute("SELECT name FROM shop_operators WHERE shop_id=? ORDER BY name", (shop_id,))
                ]
        login_method = login_method or old.get("login_method", "password")
        login_url = old.get("login_url", "") if login_url is None else login_url
        self.validate(username, password, browser, name, platform, operator, login_method, login_url)
        changed = (
            username.strip() != old["username"]
            or password != old["password"]
            or platform != old.get("platform", "pdd")
            or login_url != old.get("login_url", "")
        )
        with self.connect() as db:
            db.execute(
                "UPDATE shops SET alias=?,username=?,password=?,browser=?,platform=?,operator=?,login_method=?,login_url=?,profile_version=profile_version+?,name=? WHERE id=?",
                (
                    name.strip(),
                    username.strip(),
                    self.cipher.encrypt(password.encode()),
                    browser,
                    platform,
                    "",
                    login_method,
                    login_url,
                    int(changed),
                    "" if changed else old["name"],
                    shop_id,
                ),
            )
            from .operators import names, replace, publish

            replace(db, shop_id, names(operator))
            publish(db, [shop_id])
            if changed:
                from .table_index import remove_shop

                remove_shop(db, shop_id)
                db.execute("DELETE FROM business_cache WHERE shop_id=?", (shop_id,))
                from .collection import cleanup_shop
                from .status_cache import touch

                cleanup_shop(db, shop_id)
                touch(self, shop_id)

        if changed:
            from .status_cache import touch

            touch(self, shop_id)

    def delete(self, shop_id):
        with self.connect() as db:
            if db.execute("DELETE FROM shops WHERE id=?", (shop_id,)).rowcount != 1:
                raise ValueError("店铺不存在")
            db.execute("DELETE FROM shop_operators WHERE shop_id=?", (shop_id,))
            db.execute("DELETE FROM window_preferences WHERE shop_id=?", (shop_id,))
            db.execute("DELETE FROM debug_modes WHERE shop=?", (shop_id,))
            from .table_index import remove_shop

            remove_shop(db, shop_id)
            db.execute("DELETE FROM business_cache WHERE shop_id=?", (shop_id,))
            from .collection import cleanup_shop
            from .status_cache import touch

            cleanup_shop(db, shop_id)
            touch(self, shop_id)

            db.execute("DELETE FROM tax_records WHERE shop_id=?", (shop_id,))
            db.execute("DELETE FROM tax_history WHERE shop_id=?", (shop_id,))
            db.execute("DELETE FROM tax_reviews WHERE shop_id=?", (shop_id,))

        from .status_cache import forget

        forget(self, shop_id)

    def cache_info(self, shop_id, cache_key):
        """Scheduling only needs publication time, never the potentially huge payload."""
        with self.connect() as db:
            row = db.execute(
                "SELECT updated FROM business_cache WHERE shop_id=? AND cache_key=?", (shop_id, cache_key)
            ).fetchone()
            if not row:
                row = db.execute(
                    "SELECT updated FROM daily_archive WHERE shop_id=? AND cache_key=?", (shop_id, cache_key)
                ).fetchone()
            return {"updated": row["updated"]} if row else None

    def read_cache(self, shop_id, cache_key):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload,updated FROM business_cache WHERE shop_id=? AND cache_key=?", (shop_id, cache_key)
            ).fetchone()
            if not row:
                row = db.execute(
                    "SELECT payload,updated FROM daily_archive WHERE shop_id=? AND cache_key=?", (shop_id, cache_key)
                ).fetchone()
            if not row:
                return None
            payload = json.loads(row["payload"])
            if payload.get("_order_rows_v2"):
                payload["rows"] = [
                    {**json.loads(r["payload"]), "_order_observed": r["observed"]}
                    for r in db.execute(
                        "SELECT payload,observed FROM order_entities WHERE shop_id=? AND cache_key=? AND payload IS NOT NULL",
                        (shop_id, cache_key),
                    )
                ]
            return {"payload": payload, "updated": row["updated"]}

    def write_cache(self, shop_id, cache_key, payload):
        # Retry only this atomic publication; retain fetched rows in memory.
        for attempt in range(3):
            try:
                return self._write_cache(shop_id, cache_key, payload)
            except sqlite3.OperationalError as error:
                if not any(word in str(error).lower() for word in ("locked", "busy")) or attempt == 2:
                    raise
                time.sleep(0.25 * (attempt + 1))

    def _write_cache(self, shop_id, cache_key, payload):
        from .status_cache import invalidating

        encoded = json.dumps(payload, ensure_ascii=False)
        with invalidating(self, shop_id), self.write_connection() as db:
            # Commit order must follow `updated`: replication advances a forward-only cursor.
            if not db.execute("SELECT 1 FROM shops WHERE id=?", (shop_id,)).fetchone():
                return
            # Never move backwards: a node clock rollback (NTP correction, manual change)
            # would otherwise make fresh data look older than what the center has.
            previous = db.execute("SELECT max(updated) FROM business_cache WHERE shop_id=?", (shop_id,)).fetchone()[0]
            updated = max(time.time(), (previous or 0) + 0.001)
            if cache_key.startswith("aftersales:") and payload.get("complete"):
                from .refund_index import save as save_refunds

                save_refunds(db, shop_id, payload.get("rows", []), updated)
            from .payment_daily import save as save_payment

            save_payment(db, shop_id, cache_key, payload, updated)
            from .operations_insights import archive_home

            archive_home(db, shop_id, cache_key.split(":")[0], payload, updated)
            db.execute(
                "INSERT OR REPLACE INTO business_cache(shop_id,cache_key,payload,updated) VALUES(?,?,?,?)",
                (shop_id, cache_key, encoded, updated),
            )
            from .table_index import write

            write(db, shop_id, cache_key, payload, updated, reuse=True)
            # Summary publications refresh the corresponding business view while
            # retaining a separate scope from its complete detail snapshot.
            kind = cache_key.split(":")[1] if cache_key.startswith("summary:") else cache_key.split(":")[0]
            self.invalidate_responses(shop_id, kind, db=db)
        from .center.delivery_notify import notify

        notify(self.directory, shop_id)
        self._prune_cache(shop_id)

    def _prune_cache(self, shop_id):
        # Maintenance must not extend the atomic publication or compete with backlog.
        now = time.monotonic()
        with self._cache_prune_lock:
            if now - self._cache_pruned.get(shop_id, -float("inf")) < 60:
                return
            # Failed attempts also cool down; concurrent completions cannot all prune.
            self._cache_pruned[shop_id] = now
        state = self.writer_for(shop_id).status()
        if state["active"] or state["pending"] or state["pressure"]:
            return
        path = self._shards.path(shop_id) if self._shards else self.path
        stage = "获取清理写锁"
        try:
            from .database_writer import connection
            from .table_index import prune_batch

            # Shop-local maintenance needs no federation or node attachment. Never
            # wait behind collection/upload writers, including external processes.
            # Work in short committed slices: an interrupted slice only loses itself,
            # and the next maintenance window continues where this one stopped.
            total = time.monotonic() + CACHE_PRUNE_SECONDS
            with connection(path, timeout=0) as db:
                db.execute("PRAGMA busy_timeout=0")
                ordinary = "shop_id=? AND cache_key NOT LIKE 'orders:view-v%:%' AND cache_key NOT LIKE 'reconciliation_%' AND cache_key NOT LIKE 'tax:%' AND cache_key NOT IN (SELECT cache_key FROM collection_tasks WHERE shop_id=? AND schedule_state='periodic')"
                more = True
                first = True
                while more and time.monotonic() < total:
                    if not first and self.writer_for(shop_id).status()["pending"]:
                        break  # collection is waiting: yield immediately
                    first = False
                    db.execute("BEGIN IMMEDIATE")
                    deadline = time.monotonic() + 0.15
                    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                    try:
                        stage = "删除旧缓存"
                        removed = db.execute(
                            "DELETE FROM business_cache WHERE rowid IN (SELECT rowid FROM business_cache WHERE "
                            + ordinary
                            + " ORDER BY updated DESC LIMIT 20 OFFSET ?)",
                            (shop_id, shop_id, CACHE_LIMIT),
                        ).rowcount
                        stage = "清理列表索引"
                        more = prune_batch(db, shop_id) or removed == 20
                        db.execute("COMMIT")
                    except BaseException:
                        if db.in_transaction:
                            db.execute("ROLLBACK")
                        raise
                    finally:
                        db.set_progress_handler(None, 0)
            if more:
                # Continue soon instead of after the normal 60 s cooldown.
                with self._cache_prune_lock:
                    self._cache_pruned[shop_id] = time.monotonic() - 50
        except sqlite3.OperationalError as error:
            import logging
            from .audit_store import clean_message

            name = getattr(error, "sqlite_errorname", "")
            # A slice hitting its time budget is routine; keep real lock/IO errors visible.
            logger = logging.getLogger(__name__)
            log = logger.info if name == "SQLITE_INTERRUPT" else logger.warning
            log(
                "业务数据已提交，缓存清理本批延后 · shop=%s · 步骤=%s · %s · %s · 已完成的批次已保存",
                shop_id,
                stage,
                getattr(error, "sqlite_errorname", type(error).__name__),
                clean_message(error),
            )

    @property
    def response_revision(self):
        with self.connect() as db:
            return db.execute("SELECT coalesce(max(version),0) FROM data_versions").fetchone()[0]

    def invalidate_responses(self, shop_id=0, kind="*", db=None):
        from .data_versions import publish

        if db is not None:
            return publish(db, shop_id, kind)
        with self.connect() as connection:
            return publish(connection, shop_id, kind)

    def product_images(self, shop_id, goods_ids=None):
        # Account changes remove these caches. Never join products across shops.
        from .product_images import thumbnail, KINDS

        if goods_ids is not None:
            ids = sorted({str(g) for g in goods_ids if g not in (None, "")})
            if not ids:
                return {}
            result = {}
            with self.connect() as db:
                for offset in range(0, len(ids), 500):
                    batch = ids[offset : offset + 500]
                    # The projection already stores the resolved thumbnail; never decode row payloads here.
                    records = db.execute(
                        "SELECT r.cache_key,r.goods_id,r.thumbnail_url,r.name FROM table_rows r JOIN table_snapshots s ON r.shop_id=s.shop_id AND r.cache_key=s.cache_key WHERE r.shop_id=? AND r.thumbnail_url<>'' AND r.goods_id IN ("
                        + ",".join("?" for _ in batch)
                        + ") ORDER BY s.updated DESC,r.ordinal",
                        [shop_id, *batch],
                    )
                    for record in records:
                        if record["goods_id"] in result or record["cache_key"].split(":")[0] not in KINDS:
                            continue
                        result[record["goods_id"]] = {
                            "thumbnail_url": record["thumbnail_url"],
                            "name": record["name"] or "",
                        }
            return result
        with self.connect() as db:
            cached = db.execute(
                "SELECT cache_key,payload FROM (SELECT shop_id,cache_key,payload,updated FROM business_cache UNION ALL SELECT shop_id,cache_key,payload,updated FROM daily_archive) WHERE shop_id=? ORDER BY updated DESC",
                (shop_id,),
            ).fetchall()
        result = {}
        for cache in cached:
            if cache["cache_key"].split(":")[0] not in KINDS:
                continue
            for row in json.loads(cache["payload"]).get("rows", []):
                goods_id = str(row.get("goods_id") or "")
                url = thumbnail(row)
                if goods_id and url and goods_id not in result:
                    result[goods_id] = {"thumbnail_url": url, "name": row.get("name", "")}
        return result

    def credentials(self, shop_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()
            if row is None:
                raise ValueError("店铺不存在")
            result = dict(row)
            result["password"] = self.cipher.decrypt(result["password"]).decode()
            return result

    def rename(self, shop_id, name):
        from .shop_name import complete

        if not complete(name):
            return
        with self.connect() as db:
            db.execute("UPDATE shops SET name=? WHERE id=?", (name[:100], shop_id))
            db.execute(
                "UPDATE shops SET alias='' WHERE id=? AND (instr(alias,'*')>0 OR instr(alias,'＊')>0)", (shop_id,)
            )

    def browser(self, shop_id, kind):
        if kind not in ("system", "bundled"):
            raise ValueError("浏览器类型不正确")
        with self.connect() as db:
            db.execute("UPDATE shops SET browser=? WHERE id=?", (kind, shop_id))

    def import_legacy(self, path):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM settings WHERE key='legacy_imported'").fetchone():
                return
            content = Path(path).read_text(encoding="utf-8-sig") if Path(path).exists() else ""
            for block in re.split(r"^\[店铺\s*\d+\]\s*$", content, flags=re.M)[1:]:
                fields = dict(re.findall(r"^(店铺名称|账号|密码)=([^\r\n]*)", block, re.M))
                if fields.get("账号") and fields.get("密码"):
                    db.execute(
                        "INSERT OR IGNORE INTO shops(name,username,password) VALUES(?,?,?)",
                        (fields.get("店铺名称", ""), fields["账号"], self.cipher.encrypt(fields["密码"].encode())),
                    )
            db.execute("INSERT INTO settings(key,value) VALUES('legacy_imported','1')")
