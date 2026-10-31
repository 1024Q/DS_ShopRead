import sqlite3
import asyncio, contextvars, tempfile, threading, unittest, sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch
from backend import async_database, collection
from backend.store import Store
from backend.business import BusinessService
from backend.scheduler import Scheduler
from backend.tests.collection_fixture import assign


class SchedulerAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_snapshot_survives_scan_and_scope_plan_is_shared(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            for n in range(3):
                store.add("test" + str(n), "password")
            service = BusinessService(store, lambda _: None)
            scheduler = Scheduler(service)
            scheduler.waiting = {(99, "old"): {"stage": "unchanged"}}
            entered = asyncio.Event()
            release = asyncio.Event()

            async def scan(shop, config):
                scheduler.scan_waiting[(shop["id"], "new")] = {"stage": "new"}
                entered.set()
                await release.wait()

            scheduler.tick_shop = scan
            with patch.object(scheduler, "scopes", return_value=iter([("products", {})])) as scopes:
                task = asyncio.create_task(scheduler.tick())
                await asyncio.wait_for(entered.wait(), 3)
                self.assertEqual(scheduler.waiting, {(99, "old"): {"stage": "unchanged"}})
                release.set()
                await task
                self.assertEqual(len(scheduler.waiting), 3)
                scopes.assert_called_once()
            await service.close()

    async def test_blocked_database_writers_do_not_starve_reads_or_default_pool(self):
        release = threading.Event()
        entered = threading.Event()
        pool = ThreadPoolExecutor(1)
        token = contextvars.ContextVar("test_database_context", default="missing")
        token.set("kept")

        def block():
            entered.set()
            release.wait(3)

        try:
            with patch.object(async_database, "_writes", pool):
                blocked = asyncio.create_task(async_database.run(block))
                while not entered.is_set():
                    await asyncio.sleep(0.01)
                self.assertEqual(await asyncio.wait_for(async_database.read(token.get), 1), "kept")
                self.assertEqual(await asyncio.wait_for(asyncio.to_thread(lambda: 42), 1), 42)
                blocked.cancel()
                await asyncio.sleep(0.03)
                self.assertFalse(blocked.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await blocked
        finally:
            release.set()
            pool.shutdown()


class ExplicitStoreTests(unittest.TestCase):
    def test_retention_uses_explicit_shop_batches_and_keeps_new_runs(self):
        from backend.database_maintenance import delete_before
        from backend.shard_connection import FederatedConnection

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            ids = [store.add("first", "pw"), store.add("second", "pw")]
            store.enable_shop_databases()
            for sid in ids:
                with store.shop_db(sid, write=True) as db:
                    db.executemany(
                        "INSERT INTO collection_runs(shop_id,kind,finished,seconds,success,error_kind) VALUES(?,?,?,?,?,?)",
                        [(sid, "products", stamp, 1, 1, "") for stamp in (1, 2, 100)],
                    )
            with patch.object(FederatedConnection, "execute", side_effect=AssertionError("implicit routing")):
                self.assertEqual(delete_before(store, "collection_runs", "finished", 10, batch=1), 4)
                self.assertEqual(delete_before(store, "collection_runs", "finished", 10, batch=1), 0)
            for sid in ids:
                with store.shop_db(sid) as db:
                    self.assertEqual(db.execute("SELECT finished FROM collection_runs").fetchall()[0][0], 100)

    def test_ambiguous_cross_shop_write_rejected_without_partial_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            a = store.add("a", "pw")
            b = store.add("b", "pw")
            store.enable_shop_databases()
            for sid in (a, b):
                collection.register(store, sid, "products:x", "products", {})
            with self.assertRaisesRegex(ValueError, "跨店"):
                with store.write_connection() as db:
                    db.execute("DELETE FROM collection_tasks")
            for sid in (a, b):
                with store.shop_db(sid) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM collection_tasks").fetchone()[0], 1)
            with store.node_db() as reader:
                with self.assertRaises(sqlite3.OperationalError):
                    reader.execute("INSERT INTO settings VALUES('read-only', 'invalid')")
            with store.node_db(write=True) as db:
                self.assertTrue(db.admitted)
                self.assertTrue(db.in_transaction)


if __name__ == "__main__":
    unittest.main()
