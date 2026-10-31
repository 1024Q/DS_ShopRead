import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from backend import collection, home_progress
from backend.business import BusinessService
from backend.browser import Session
from backend.store import Store
from backend.summary_publication import key, merge, publish
from backend.center.database import Database
from backend.center.business import CentralBusiness
from backend.center.business_events import heads, notification
from backend.center.server import create_app


class SummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_detail_failure_preserves_committed_summary_full_rows_and_checkpoint(self):
        from backend.promotions import options_for
        from backend.collection_checkpoint import current, Checkpoint

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("fixture", "pw")
            shop = store.list()[0]
            item = Session(shop, store)
            item.state = "logged_in"
            item.window = AsyncMock()
            service = BusinessService(store, lambda _: item)
            options = options_for()
            cache_key = service.key("ad_reports", options, shop)
            old = {"rows": [{"id": "old-row"}], "metrics": {"ad_spend": 1}, "complete": True}
            store.write_cache(sid, cache_key, copy.deepcopy(old))
            old_success = time.time() - 120
            with store.connect() as db:
                db.execute(
                    "UPDATE business_cache SET updated=? WHERE shop_id=? AND cache_key=?", (old_success, sid, cache_key)
                )
                db.execute(
                    "UPDATE table_snapshots SET updated=? WHERE shop_id=? AND cache_key=?",
                    (old_success, sid, cache_key),
                )

            async def reader(session, kind, scope, *args):
                checkpoint = current.get()
                checkpoint.data["proof"] = "continuation retained"
                checkpoint.save()
                await publish(
                    kind,
                    scope,
                    {
                        "metrics": {"ad_spend": 7, "ad_revenue": 21, "total_spend": 8},
                        "rows": [],
                        "summary_complete": True,
                        "complete": False,
                    },
                )
                self.assertEqual(store.read_cache(sid, cache_key)["payload"]["rows"], old["rows"])
                self.assertEqual(store.read_cache(sid, key(cache_key))["payload"]["metrics"]["ad_spend"], 7)
                raise ValueError("detail failed")

            try:
                collection.register(store, sid, cache_key, "ad_reports", options)
                with patch("backend.incremental_collection.read", side_effect=reader):
                    service.enqueue(shop, item, "ad_reports", options, force=True)
                    await asyncio.gather(*list(service.jobs.values()))
                state = collection.state(store, sid, cache_key)
                self.assertEqual(state["failures"], 1)
                self.assertEqual(state["last_success"], old_success)
                self.assertEqual(Checkpoint(store, sid, cache_key, {}).data["proof"], "continuation retained")
                result = service.data("ad_reports", options)
                self.assertEqual(result["metrics"][0]["ad_spend"], 7)
                self.assertEqual(result["rows"][0]["id"], "old-row")
                self.assertFalse(result["sources"][0]["complete"])
                self.assertTrue(result["sources"][0]["details_pending"])
                module = next(m for m in home_progress.read(service)["rows"][0]["modules"] if m["kind"] == "ad_reports")
                self.assertTrue(module["today_updated"])
                self.assertTrue(module["details_pending"])
                self.assertFalse(module["overdue"])
                self.assertIn("detail failed", module["error"])
            finally:
                await service.close()

    def test_later_full_result_supersedes_summary_and_old_summary_cannot_replace_it(self):
        brief = {"updated": 10, "payload": {"summary_complete": True, "metrics": {"ad_spend": 7}, "rows": []}}
        complete = {"updated": 11, "payload": {"complete": True, "metrics": {"ad_spend": 8}, "rows": [{"id": "new"}]}}
        self.assertEqual(merge("ad_reports", complete, brief), complete)
        self.assertEqual(merge("orders", complete, brief), complete)

    def test_coverage_and_summary_reads_are_scoped_after_shop_database_migration(self):
        from backend.home_progress_data import read
        from backend.summary_publication import read_many
        from backend.shard_connection import FederatedConnection

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            ids = [store.add(str(i), "pw") for i in range(2)]
            for sid in ids:
                store.write_cache(sid, "ad_reports:wanted", {"complete": True, "metrics": {"ad_spend": 1}, "rows": []})
                store.write_cache(
                    sid,
                    key("ad_reports:wanted"),
                    {
                        "summary_complete": True,
                        "complete": False,
                        "metrics": {"ad_spend": sid},
                        "_sync": {"snapshot_started_at": 123},
                    },
                )
                store.write_cache(sid, "ad_reports:other", {"complete": True, "metrics": {"ad_spend": 99}})
                collection.register(store, sid, "ad_reports:wanted", "ad_reports", {})
            wanted = [(sid, "ad_reports:wanted") for sid in ids]
            expected = read(store, wanted)
            expected_summaries = read_many(store, dict(wanted))
            store.enable_shop_databases()
            with patch.object(FederatedConnection, "read_all", side_effect=AssertionError("unscoped shop read")):
                self.assertEqual(read(store, wanted), expected)
                self.assertEqual(read_many(store, dict(wanted)), expected_summaries)
                one = read(store, wanted[:1])
                self.assertEqual({sid for sid, _ in one[1]}, {ids[0]})

    async def test_committed_change_wakes_listener_but_heartbeat_does_not(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database("sqlite:///" + str(Path(folder) / "center.db"))
            try:
                node = db.register("test")["id"]
                generation = db.changes.revision()
                db.ingest(node, [{"shop": "0", "scope": "collector_status", "updated": 1, "payload": {}}])
                self.assertEqual(db.changes.revision(), generation)
                task = asyncio.create_task(db.changes.wait(generation, 10))
                await asyncio.sleep(0)
                await asyncio.to_thread(
                    db.ingest,
                    node,
                    [
                        {
                            "shop": "1",
                            "scope": "summary:ad_reports:test",
                            "updated": 2,
                            "payload": {"summary_complete": True},
                        }
                    ],
                )
                await asyncio.wait_for(task, 0.5)
                current = db.changes.revision()
                with patch.object(db, "upsert", side_effect=ValueError("rollback")):
                    with self.assertRaises(ValueError):
                        db.ingest(node, [{"shop": "1", "scope": "overview:test", "updated": 3, "payload": {}}])
                self.assertEqual(db.changes.revision(), current)
            finally:
                db.engine.dispose()


class CenterSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database("sqlite:///" + str(Path(self.temp.name) / "center.db"))
        self.root = patch("backend.center.report_exports.ROOT", Path(self.temp.name) / "exports")
        self.root.start()
        self.node = self.db.register("test")["id"]
        self.db.ingest(
            self.node,
            [
                {
                    "shop": "1",
                    "scope": "shop_metadata",
                    "updated": 1,
                    "payload": {"name": "fixture", "platform": "pdd", "state": "sleeping"},
                }
            ],
        )

    def tearDown(self):
        self.root.stop()
        self.db.engine.dispose()
        self.temp.cleanup()

    def test_center_read_uses_new_summary_and_preserves_old_detail_rows(self):
        from backend.promotions import options_for

        service = CentralBusiness(self.db)
        shop = self.db.shops()[0]
        options = options_for()
        normal = service.key("ad_reports", options, shop)
        before = heads(self.db)
        self.db.ingest(
            self.node,
            [
                {"shop": "1", "scope": normal, "updated": 2, "payload": {"rows": [{"id": "one"}], "complete": True}},
                {
                    "shop": "1",
                    "scope": key(normal),
                    "updated": 3,
                    "payload": {
                        "rows": [], "metrics": {"ad_spend": 5, "total_spend": 6},
                        "summary_complete": True, "details_on_demand": True,
                    },
                },
            ],
        )
        result = service.data("ad_reports", options)
        self.assertEqual(result["metrics"][0]["ad_spend"], 5)
        self.assertEqual(result["rows"][0]["id"], "one")
        self.assertTrue(result["sources"][0]["details_pending"])
        self.assertTrue(result["sources"][0]["details_on_demand"])
        self.assertTrue(result["sources"][0]["details_complete"])
        self.assertEqual(result["sources"][0]["details_updated_at"], 2)
        self.assertEqual(result["sources"][0]["summary_updated_at"], 3)
        self.assertFalse(result["sources"][0]["complete"])
        self.assertIsNone(result["sources"][0]["error"])
        self.assertIsNotNone(result["sources"][0]["center_received_at"])
        self.assertEqual(
            notification(before, heads(self.db), self.db.shops())["changes"],
            [{"kind": "ad_reports", "shop_id": shop["id"], "platform": "pdd"}],
        )

    def test_upload_invalidates_progress_and_next_get_returns_new_projection(self):
        import hashlib

        scope = "response:" + hashlib.sha256(b"home/progress?").hexdigest()
        old = {"rows": [{"shop_id": 1, "total": 1, "updated": 0, "modules": []}]}
        self.db.ingest(self.node, [{"shop": "0", "scope": scope, "updated": time.time(), "payload": {"value": old}}])
        app = create_app(self.db, "a" * 40)
        with TestClient(app) as client:
            app.state.hub.peers[self.node] = object()
            fresh = {"rows": [{"shop_id": 1, "total": 1, "updated": 1, "modules": []}]}
            self.db.ingest(self.node, [{"shop": "1", "scope": "overview:new", "updated": time.time(), "payload": {}}])
            with patch.object(
                app.state.hub, "request", AsyncMock(return_value={"status": 200, "content": json.dumps(fresh).encode()})
            ):
                result = client.get("/api/home/progress", headers={"Authorization": "Bearer " + "a" * 40}).json()
                self.assertEqual(result["updated_modules"], 1)
                self.assertFalse(result["progress_freshness"][0]["stale"])
            app.state.hub.peers.clear()
