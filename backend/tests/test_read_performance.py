import sqlite3, tempfile, time, unittest
from types import SimpleNamespace
from unittest.mock import patch
from backend.query_cache import QueryCache
from backend.store import Store
from backend import collection
from backend.business import BusinessService, options_for
from backend.scheduler import Scheduler
from backend.system_status import summary
from backend.browser import Session


class CacheTests(unittest.TestCase):
    def test_expiry_revision_and_memory_bound(self):
        cache = QueryCache(ttl=2, max_bytes=5, max_entries=2)
        with patch("backend.query_cache.time.monotonic", return_value=10):
            cache.put(("reviews", "shop=1"), 1, b"123", "application/json")
            self.assertIsNotNone(cache.get(("reviews", "shop=1"), 1))
            self.assertIsNone(cache.get(("reviews", "shop=2"), 1))
            self.assertIsNone(cache.get(("reviews", "shop=1"), 2))
            cache.put("a", 2, b"123", "application/json")
            cache.put("b", 2, b"456", "application/json")
            self.assertIsNone(cache.get("a", 2))
            self.assertLessEqual(cache.size, 5)
        with patch("backend.query_cache.time.monotonic", return_value=13):
            self.assertIsNone(cache.get("b", 2))

    def test_http_cache_checks_auth_and_invalidates_on_publish_and_mutation(self):
        from backend import main
        from fastapi import Depends
        from fastapi.routing import APIRoute
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            sid = store.add("test", "pw")
            calls = []

            def read():
                calls.append(1)
                return {"version": getattr(store, "response_revision", 0)}

            def mutate():
                return {"ok": True}

            routes = [
                APIRoute("/api/data/performance-probe", read, methods=["GET"], dependencies=[Depends(main.authorize)]),
                APIRoute("/api/performance-probe", mutate, methods=["POST"], dependencies=[Depends(main.authorize)]),
            ]
            main.app.router.routes[0:0] = routes
            try:
                with patch.object(main, "store", store), patch.object(main, "query_cache", QueryCache()):
                    client = TestClient(main.app)
                    headers = {"Authorization": "Bearer " + main.TOKEN}
                    try:
                        self.assertEqual(
                            client.get("/api/data/performance-probe", headers=headers).headers["x-query-cache"], "miss"
                        )
                        self.assertEqual(
                            client.get("/api/data/performance-probe", headers=headers).headers["x-query-cache"], "hit"
                        )
                        self.assertEqual(client.get("/api/data/performance-probe").status_code, 401)
                        store.write_cache(sid, "reviews:x", {"rows": [], "complete": True})
                        self.assertEqual(
                            client.get("/api/data/performance-probe", headers=headers).headers["x-query-cache"], "miss"
                        )
                        client.post("/api/performance-probe", headers=headers)
                        self.assertEqual(
                            client.get("/api/data/performance-probe", headers=headers).headers["x-query-cache"], "miss"
                        )
                        self.assertEqual(len(calls), 3)
                    finally:
                        client.close()
            finally:
                for route in routes:
                    main.app.router.routes.remove(route)


class ReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("test", "pw")
        self.item = Session(self.store.list()[0], self.store)
        self.item.state = "logged_in"
        self.service = BusinessService(self.store, lambda _: self.item)

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    async def test_scheduler_does_not_load_snapshots_or_enqueue_not_due_tasks(self):
        scheduler = Scheduler(self.service)
        config = {k: "off" for k, _ in scheduler.scopes()}
        config["reviews"] = "realtime"
        from backend.tests.collection_fixture import assign

        assign(self.store, {"reviews@base": "realtime"}, reset=True)
        opts = options_for("reviews")
        key = self.service.key("reviews", opts, self.item.shop)
        self.store.write_cache(self.sid, key, {"rows": [], "complete": True})
        collection.register(self.store, self.sid, key, "reviews", opts, self.store.cache_info(self.sid, key))
        with (
            patch.object(self.store, "read_cache", side_effect=AssertionError("decoded full snapshot")),
            patch.object(self.service, "enqueue_async") as enqueue,
        ):
            await scheduler.tick()
            enqueue.assert_not_called()

    async def test_metadata_read_and_header_summary_do_not_decode_business_json(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO business_cache VALUES(?,?,?,?)", (self.sid, "reviews:x", "not-json", time.time()))
        self.assertIsNotNone(self.store.cache_info(self.sid, "reviews:x"))
        result = summary(self.service)
        self.assertEqual(result["summary"]["cached"], 1)
        self.assertNotIn("sources", result)
        self.assertNotIn("not-json", str(result))

    async def test_sharded_summary_aggregates_each_shop_without_federation_or_payload_reads(self):
        from backend.shard_connection import FederatedConnection

        other = self.store.add("second", "pw")
        self.store.enable_shop_databases()
        with self.store.connect() as db:
            for sid in (self.sid, other):
                db.executemany(
                    "INSERT INTO business_cache VALUES(?,?,?,?)",
                    [(sid, "reviews:scope-" + str(i), "not-json", i + 1) for i in range(200)],
                )
                db.execute("INSERT INTO daily_archive VALUES(?,?,?,?)", (sid, "reviews:archive", "not-json", 500))
                db.execute("INSERT INTO business_cache VALUES(?,?,?,?)", (sid, "products", "not-json", 100))
        old_db = FederatedConnection.db

        def guarded(connection, sid=None):
            db = old_db(connection, sid)
            db.set_authorizer(
                lambda action, table, column, *args: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ
                    and table in ("business_cache", "daily_archive")
                    and column == "payload"
                    else sqlite3.SQLITE_OK
                )
            )
            return db

        with (
            patch.object(
                FederatedConnection, "read_all", side_effect=AssertionError("status must not materialize all shop rows")
            ),
            patch.object(FederatedConnection, "db", guarded),
        ):
            result = summary(self.service)
            details = summary(self.service, "cached")
        self.assertEqual(result["summary"]["cached"], 4)
        self.assertEqual(details["total"], 4)

    async def test_summary_uses_newest_cache_or_archive_per_shop_to_clear_old_error(self):
        other = self.store.add("second", "pw")
        self.store.enable_shop_databases()
        for sid in (self.sid, other):
            collection.register(self.store, sid, "reviews:failed", "reviews", {})
        with self.store.connect() as db:
            for sid in (self.sid, other):
                db.execute("UPDATE collection_tasks SET last_error=?,retry_at=? WHERE shop_id=?", ("旧失败", 400, sid))
                db.execute("INSERT INTO business_cache VALUES(?,?,?,?)", (sid, "reviews:recent", "not-json", 200))
            db.execute("INSERT INTO daily_archive VALUES(?,?,?,?)", (self.sid, "reviews:archive", "not-json", 500))
        result = summary(self.service)
        self.assertEqual(result["summary"]["cached"], 2)
        self.assertEqual(result["summary"]["errors"], 1)

    async def test_sharded_source_headers_do_not_federate_business_caches(self):
        from backend.shard_connection import FederatedConnection
        from backend.system_status import snapshot

        other = self.store.add("second", "pw")
        self.store.enable_shop_databases()
        self.store.write_cache(self.sid, "reviews:current", {"rows": [{"id": "a"}], "complete": True})
        self.store.write_cache(other, "reviews:current", {"rows": [{"id": "b"}, {"id": "c"}], "complete": True})
        original = FederatedConnection.read_all

        def bounded(connection, sql, params, touched):
            if {"business_cache", "daily_archive"} & set(touched):
                raise AssertionError("source headers must remain shop-local")
            return original(connection, sql, params, touched)

        with patch.object(FederatedConnection, "read_all", bounded):
            result = snapshot(self.service)
        counts = {row["shop_id"]: row["loaded_count"] for row in result["sources"] if row["kind"] == "reviews"}
        self.assertEqual(counts, {self.sid: 1, other: 2})

    async def test_publication_invalidates_response_revision_and_wal_enabled(self):
        before = getattr(self.store, "response_revision", 0)
        self.store.write_cache(self.sid, "reviews:x", {"rows": [], "complete": True})
        self.assertGreater(self.store.response_revision, before)
        with self.store.connect() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")


class AsyncDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_waits_for_write_without_blocking_loop(self):
        import asyncio, threading
        from backend.async_database import run

        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def write():
            started.set()
            release.wait(3)
            finished.set()

        task = asyncio.create_task(run(write))
        try:
            for _ in range(200):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(started.is_set())
            task.cancel()
            await asyncio.sleep(0.03)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(finished.is_set())
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
