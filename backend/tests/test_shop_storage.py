import asyncio, json, sqlite3, tempfile, threading, unittest
from pathlib import Path
from contextlib import closing
from backend.store import Store
from backend.shop_storage import rollback, merge_snapshot
from backend.center.shop_delivery import ShopDeliveryGroup
from backend.center.order_delivery import OrderDelivery
from backend import order_storage, order_plan, collection
from datetime import datetime


class ShopStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.a = self.store.add("first", "secret")
        self.b = self.store.add("second", "secret")
        self.store.write_cache(self.a, "products:test", {"rows": [{"id": "old"}]})
        with self.store.connect() as db:
            db.execute("INSERT INTO settings VALUES('collection_policy','unchanged')")
        self.store.enable_shop_databases()

    def tearDown(self):
        self.temp.cleanup()

    def test_scheduling_batch_reads_only_requested_shards(self):
        from unittest.mock import patch
        from backend.business import scheduling_states, BusinessService
        from backend.shard_connection import FederatedConnection

        with self.store.connect() as db:
            db.execute("UPDATE settings SET value='{}' WHERE key='collection_policy'")
        shops = {s["id"]: s for s in self.store.list()}
        service = BusinessService(self.store, lambda _: None)
        candidates = []
        for sid in (self.a, self.b):
            scope = {}
            key = service.key("products", scope, shops[sid])
            collection.register(self.store, sid, key, "products", scope)
            candidates.append((shops[sid], None, "products", scope, {}))
        with patch.object(FederatedConnection, "read_all", side_effect=AssertionError("cross-shop scan")):
            caches, tasks = scheduling_states(self.store, [(self.a, "products:test")])
            self.assertIn((self.a, "products:test"), caches)
            plans = service.prepare_scheduled(candidates, service.queue_config)
            self.assertEqual([p[3][4]["shop_id"] for p in plans], [self.a, self.b])

    def test_isolation_and_rollback_preserve_new_rows_and_policy(self):
        self.store.write_cache(self.a, "products:new", {"rows": [{"id": "a"}]})
        self.store.write_cache(self.b, "products:new", {"rows": [{"id": "b"}]})
        with self.store.connect() as db:
            for sid in (self.a, self.b):
                db.execute(
                    "INSERT INTO collection_runs(shop_id,kind,finished,seconds,success,error_kind) VALUES(?,?,?,?,?,?)",
                    (sid, "products", 10, 1, 1, ""),
                )
            self.assertEqual(db.execute("SELECT count(*) FROM business_cache").fetchone()[0], 3)
        for sid in (self.a, self.b):
            with closing(sqlite3.connect(self.store._shards.path(sid))) as db:
                self.assertEqual({r[0] for r in db.execute("SELECT DISTINCT shop_id FROM business_cache")}, {sid})
                self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='shops'").fetchone())
        rollback(self.root)
        restored = Store(self.root)
        self.assertEqual(restored.read_cache(self.b, "products:new")["payload"]["rows"][0]["id"], "b")
        with restored.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM collection_runs").fetchone()[0], 2)
            self.assertEqual(
                db.execute("SELECT value FROM settings WHERE key='collection_policy'").fetchone()[0], "unchanged"
            )
        # Re-activation must use current merged rows rather than stale shard files.
        restored.write_cache(self.a, "products:after-rollback", {"rows": []})
        restored.enable_shop_databases()
        self.assertIsNotNone(restored.read_cache(self.a, "products:after-rollback"))

    def test_one_shop_write_does_not_block_another(self):
        ready = threading.Event()
        release = threading.Event()
        errors = []

        def hold():
            try:
                with self.store.write_connection() as db:
                    db.execute("UPDATE business_cache SET updated=updated+1 WHERE shop_id=?", (self.a,))
                    ready.set()
                    release.wait(5)
            except Exception as e:
                errors.append(e)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(ready.wait(3))
        try:
            self.store.write_cache(self.b, "products:new", {"rows": []})
            self.assertTrue(thread.is_alive())
            self.assertEqual(self.store.writer_for(self.b).status()["pending"], 0)
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(errors, [])

    def test_order_stream_and_delivery_are_per_shop(self):
        day = datetime.now(order_plan.BEIJING).date().isoformat()
        for shop in self.store.list():
            order_storage.publish(
                self.store,
                shop,
                order_plan.day_scope(day, "recent"),
                {
                    "rows": [{"id": "same-id", "created_at": day + " 10:00:00", "status": "已成团，待发货"}],
                    "complete": True,
                    "_sync": {"snapshot_started_at": 10},
                },
                True,
            )
        group = ShopDeliveryGroup(self.root, "orders", ["url", "node"])
        group.discover()
        batches = [child.batch() for child in group.children.values()]
        self.assertEqual(len({b["stream"] for b in batches}), 2)
        self.assertEqual([len(b["rows"]) for b in batches], [1, 1])
        for lane in ("business", "auxiliary"):
            group = ShopDeliveryGroup(self.root, lane, ["url", "node"])
            group.discover()
            for sid, child in group.children.items():
                self.assertTrue(all(str(r["shop"]) == sid for r in child.collect()))
        rollback(self.root)
        batch = OrderDelivery(self.root).batch()
        self.assertEqual(len(batch["rows"]), 2)
        self.assertEqual(len({r["revision"] for r in batch["rows"]}), 2)

    def test_snapshot_includes_shard_only_rows(self):
        self.store.write_cache(self.b, "products:new", {"rows": [{"id": "new"}]})
        target = self.root / "backup.sqlite3"
        merge_snapshot(self.root, target)
        with closing(sqlite3.connect(target)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM business_cache").fetchone()[0], 2)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_checkpoint_migration_and_restart(self):
        from backend.collection_checkpoint import Checkpoint

        collection.register(self.store, self.a, "orders:checkpoint", "orders", {})
        checkpoint = Checkpoint(self.store, self.a, "orders:checkpoint", {})
        checkpoint.data["parts"]["p"] = {"rows": {"one": {"id": "one"}}, "ids": ["one"], "loaded": 1, "total": 2}
        checkpoint.mark("p", [{"id": "one"}])
        checkpoint.save()
        reopened = Store(self.root)
        saved = Checkpoint(reopened, self.a, "orders:checkpoint", {})
        self.assertEqual(saved.data["parts"]["p"]["rows"]["one"]["id"], "one")
        with reopened.connect() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM collection_tasks WHERE shop_id=?", (self.a,)).fetchone()[0], 1
            )

    def test_failed_migration_does_not_activate_incomplete_copy(self):
        from unittest.mock import patch
        from backend.shop_storage import ShopStorage

        sid = self.store.add("third", "secret")
        with patch("backend.shop_storage.digest", side_effect=[{"rows": 1}, {"rows": 2}]):
            with self.assertRaises(RuntimeError):
                self.store._shards.migrate_shop(sid)
        self.assertNotIn(sid, self.store._shards.paths)
        self.store._shards.migrate_shop(sid)
        self.assertIn(sid, self.store._shards.paths)

    def test_per_shop_upload_is_idempotent_at_center(self):
        from backend.center.database import Database, order_rows
        from backend.center.order_api import Changes, ingest
        from sqlalchemy import select

        center = Database("sqlite:///" + str(self.root / "center.sqlite3"))
        node = center.register("fixture")["id"]
        try:
            day = datetime.now(order_plan.BEIJING).date().isoformat()
            for shop in self.store.list():
                order_storage.publish(
                    self.store,
                    shop,
                    order_plan.day_scope(day, "recent"),
                    {
                        "rows": [{"id": "same", "created_at": day + " 10:00:00"}],
                        "complete": True,
                        "_sync": {"snapshot_started_at": 10},
                    },
                    True,
                )
            group = ShopDeliveryGroup(self.root, "orders", ["url", node])
            group.discover()
            for child in group.children.values():
                batch = Changes(**child.batch())
                ack = ingest(center, node, batch)
                self.assertEqual(ingest(center, node, batch), ack)
            with center.engine.connect() as db:
                self.assertEqual(len(db.execute(select(order_rows)).all()), 2)
        finally:
            center.engine.dispose()

    def test_updater_merges_data_before_old_code_is_restored(self):
        from backend.collector_update import rollback as rollback_release, write_json

        root = self.root / "release"
        data = root / ".data"
        data.mkdir(parents=True)
        store = Store(data)
        sid = store.add("fixture", "pw")
        store.enable_shop_databases()
        store.write_cache(sid, "products:new", {"rows": [{"id": "new"}]})
        updates = data / "updates"
        (updates / "backup-test").mkdir(parents=True)
        write_json(updates / "journal.json", {"backup": "backup-test", "files": {"backend/shop_storage.py": False}})
        rollback_release(root)
        restored = Store(data)
        self.assertFalse(restored._shards)
        self.assertEqual(restored.read_cache(sid, "products:new")["payload"]["rows"][0]["id"], "new")

    def test_migration_refuses_a_running_collector(self):
        from backend.collector_lease import exclusive

        other = self.root / "guard"
        other.mkdir()
        store = Store(other)
        store.add("guard", "pw")
        with exclusive(other):
            with self.assertRaises(OSError):
                store.enable_shop_databases()
        self.assertFalse((other / "shop-storage.json").exists())

    def test_revisions_fit_browser_integer_precision(self):
        from backend.data_versions import publish

        with self.store.connect() as db:
            first = publish(db, self.a, "products")
            second = publish(db, self.a, "products")
        self.assertGreater(second, first)
        self.assertLessEqual(second, 2**53 - 1)

    def test_cross_shop_filters_keep_sqlite_numeric_affinity(self):
        self.store.write_cache(self.b, "products:new", {"rows": []})
        with self.store.connect() as db:
            count = db.execute(
                "SELECT count(*) FROM business_cache WHERE shop_id IN (?,?)", (str(self.a), str(self.b))
            ).fetchone()[0]
        self.assertEqual(count, 2)
