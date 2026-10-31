import asyncio, json, sqlite3, tempfile, time, unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import httpx
from backend import refund_index, collector_update, collection_errors
from backend.store import Store
from backend.center.replication_worker import Delivery
from backend.center.shop_delivery import ShopDeliveryGroup


class RefundSchemaTests(unittest.TestCase):
    def test_old_shards_repair_and_restart_preserve_isolation_and_newer_values(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            a = store.add("a", "secret")
            b = store.add("b", "secret")
            store.enable_shop_databases()
            stamp = time.time()
            # Old nodes had a lazily-created node table but no matching shop table.
            for sid in (a, b):
                with closing(sqlite3.connect(store._shards.path(sid))) as db, db:
                    db.execute("DROP TABLE refund_case_index")
            with closing(sqlite3.connect(store.path)) as db, db:
                for sid in (a, b):
                    db.execute("INSERT INTO refund_case_index VALUES(?,?,?,?,?)", (sid, "case", "order", sid, stamp))
            store = Store(folder)
            for sid in (a, b):
                with closing(sqlite3.connect(store._shards.path(sid))) as db:
                    self.assertEqual(
                        db.execute("SELECT shop_id,amount FROM refund_case_index").fetchall(), [(sid, float(sid))]
                    )
            row = {"id": "case", "order_number": "order", "amount": 12, "status": "退款成功"}
            with store.connect() as db:
                refund_index.save(db, a, [row], stamp + 1)
            store = Store(folder)
            self.assertEqual(
                refund_index.lookup(store, a, [{"aftersale_id": "case", "order_number": "order"}], stamp + 2)["case"][
                    "amount"
                ],
                12,
            )
            self.assertEqual(
                refund_index.lookup(store, b, [{"aftersale_id": "case", "order_number": "order"}], stamp + 2)["case"][
                    "amount"
                ],
                b,
            )
            from backend.shop_storage import merge_snapshot

            target = Path(folder) / "rollback.sqlite3"
            merge_snapshot(folder, target)
            with closing(sqlite3.connect(target)) as db:
                self.assertEqual(
                    db.execute("SELECT amount FROM refund_case_index WHERE shop_id=?", (a,)).fetchone()[0], 12
                )

    def test_database_error_guidance_does_not_suggest_browser_recovery(self):
        error = sqlite3.OperationalError("attempt to write a readonly database")
        category, blocked = collection_errors.classify(error)
        self.assertEqual(category, "database_error")
        message = collection_errors.describe(error)
        self.assertIn("只读", message)
        self.assertIn("无需重新登录", collection_errors.guidance("orders", message, category, blocked)["action"])


class JsonStateTests(unittest.TestCase):
    def test_transient_permission_retry_and_permanent_failure_preserve_previous_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            collector_update.write_json(path, {"old": 1})
            original = Path.replace
            attempts = []

            def replace(source, target):
                attempts.append(source)
                if len(attempts) < 3:
                    raise PermissionError("in use")
                return original(source, target)

            with patch.object(Path, "replace", replace), patch("backend.collector_update.time.sleep"):
                collector_update.write_json(path, {"new": 2})
            self.assertEqual(json.loads(path.read_text()), {"new": 2})
            with (
                patch.object(Path, "replace", side_effect=PermissionError("readonly")),
                patch("backend.collector_update.time.sleep"),
            ):
                with self.assertRaises(PermissionError):
                    collector_update.write_json(path, {"bad": 3})
            self.assertEqual(json.loads(path.read_text()), {"new": 2})
            self.assertFalse(list(Path(folder).glob("*.tmp")))

    def test_concurrent_writers_use_distinct_temp_files_and_leave_valid_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            original = Path.replace
            names = []

            def replace(source, target):
                names.append(str(source))
                return original(source, target)

            with patch.object(Path, "replace", replace), ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(lambda n: collector_update.write_json(path, {"n": n}), range(12)))
            self.assertEqual(len(set(names)), 12)
            self.assertIn(json.loads(path.read_text())["n"], range(12))
            self.assertFalse(list(Path(folder).glob("*.tmp")))


class DeliveryStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_upload_stays_successful_if_status_file_is_locked(self):
        with tempfile.TemporaryDirectory() as folder:
            worker = Delivery(Path(folder), "business", ["center", "node"])
            worker.pending = 1
            row = {"shop": 1, "scope": "orders:test", "updated": 1, "payload": "{}"}
            async with httpx.AsyncClient(
                base_url="http://center", transport=httpx.MockTransport(lambda r: httpx.Response(200))
            ) as client:
                with patch("backend.center.delivery_state.write_json", side_effect=PermissionError("locked")):
                    await worker.send(client, [row])
            self.assertEqual(worker.acks[worker.key(1, "orders:test")], 1)
            self.assertFalse(worker.failures)
            self.assertEqual(worker.pending, 0)
            worker.save()
            self.assertEqual(json.loads(worker.file.read_text())["acks"], worker.acks)

    async def test_group_keeps_uploading_when_aggregate_cannot_replace_file(self):
        with tempfile.TemporaryDirectory() as folder:
            group = ShopDeliveryGroup(folder, "business", ["center", "node"])
            child = Delivery(Path(folder) / "shop", "business", ["center", "node"])
            group.children = {1: child}
            rounds = []

            async def tick(*args):
                rounds.append(1)
                group.wake.set()
                if len(rounds) == 3:
                    raise asyncio.CancelledError

            with (
                patch.object(group, "discover"),
                patch.object(group, "tick", side_effect=tick),
                patch("backend.center.delivery_state.write_json", side_effect=PermissionError("locked")),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(group.run(None), 2)
            self.assertEqual(len(rounds), 3)
