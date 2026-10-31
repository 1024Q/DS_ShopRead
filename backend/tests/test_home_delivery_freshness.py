import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from fastapi.testclient import TestClient
from backend import attention, home_progress
from backend.business import BusinessService, options_for
from backend.center.business import CentralBusiness
from backend.store import Store
from backend.center.database import Database
from backend.center.server import create_app
from backend.center.replication_worker import Delivery
from backend.center.business_events import heads, notification
from backend.center.freshness import describe, configurations
from backend.center.runtime_policy import read as read_policy


class HomeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolved_attention_reuploads_without_advancing_full_scope_clock(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("fixture", "test")
            shop = store.list()[0]
            item = SimpleNamespace(
                shop=shop, state="logged_in", retired=False, visible=False, lock=asyncio.Lock(), control_until=0
            )
            service = BusinessService(store, lambda _: item)

            def data(rows, started):
                return {"rows": rows, "complete": True, "_sync": {"snapshot_started_at": started}}

            try:
                attention.publish(
                    store,
                    sid,
                    "tickets",
                    data([{"id": "one", "status": "待处理", "attention_status": "2小时内将逾期"}], time.time() - 86400),
                )
                worker = Delivery(Path(folder), "auxiliary", ["test", "node"])
                old = next(r for r in worker.collect() if r["scope"] == "attention:tickets")
                worker.commit([old])
                attention.reconcile(store, sid, "tickets", data([{"id": "one", "status": "已完结"}], time.time()))
                fresh = next(r for r in worker.collect() if r["scope"] == "attention:tickets")
                self.assertGreater(fresh["updated"], old["updated"])
                payload = json.loads(fresh["payload"])
                self.assertEqual(payload["rows"], [])
                self.assertEqual(payload["_sync"]["full_snapshot_updated_at"], old["updated"])
                module = next(m for m in home_progress.read(service)["rows"][0]["modules"] if m["kind"] == "tickets")
                self.assertFalse(module["today_updated"])
                worker.commit([fresh])
                # Simulate a pre-upgrade snapshot with a locally repaired payload
                # and an already acknowledged timestamp: startup repairs it once.
                payload["_sync"].pop("full_snapshot_updated_at")
                with store.connect() as db:
                    db.execute(
                        "UPDATE attention_snapshots SET payload=? WHERE shop_id=? AND kind=?",
                        (json.dumps(payload), sid, "tickets"),
                    )
                self.assertFalse(any(r["scope"] == "attention:tickets" for r in worker.collect()))
                attention.initialize(store)
                repaired = next(r for r in worker.collect() if r["scope"] == "attention:tickets")
                self.assertEqual(json.loads(repaired["payload"])["_sync"]["full_snapshot_updated_at"], fresh["updated"])
                worker.commit([repaired])
                attention.initialize(store)
                self.assertFalse(any(r["scope"] == "attention:tickets" for r in worker.collect()))
            finally:
                await service.close()


class CenterFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database("sqlite:///" + str(Path(self.temp.name) / "center.sqlite"))
        self.root_patch = patch("backend.center.report_exports.ROOT", Path(self.temp.name) / "exports")
        self.root_patch.start()
        self.node = self.db.register("node-a")["id"]
        self.db.ingest(
            self.node,
            [
                {
                    "shop": "1",
                    "scope": "shop_metadata",
                    "updated": 1,
                    "payload": {"name": "shop", "platform": "pdd", "state": "logged_in"},
                }
            ],
        )

    def tearDown(self):
        self.root_patch.stop()
        self.db.engine.dispose()
        self.temp.cleanup()

    def test_heartbeats_do_not_change_business_revision_and_updates_are_scoped(self):
        old = heads(self.db)
        self.db.ingest(
            self.node,
            [
                {"shop": "0", "scope": "collector_status", "updated": 2, "payload": {}},
                {"shop": "0", "scope": "response:test", "updated": 3, "payload": {}},
            ],
        )
        self.assertEqual(heads(self.db), old)
        self.db.ingest(
            self.node,
            [
                {
                    "shop": "1",
                    "scope": "shop_metadata",
                    "updated": 5,
                    "payload": {"name": "shop", "platform": "pdd", "state": "logged_in"},
                }
            ],
        )
        self.assertEqual(heads(self.db), old)
        self.db.ingest(
            self.node,
            [
                {
                    "shop": "1",
                    "scope": "shop_metadata",
                    "updated": 6,
                    "payload": {"name": "renamed", "platform": "pdd", "state": "logged_in"},
                }
            ],
        )
        self.assertEqual(notification(old, heads(self.db), self.db.shops())["changes"][0]["kind"], "*")
        old = heads(self.db)
        self.db.ingest(self.node, [{"shop": "1", "scope": "attention:tickets", "updated": 4, "payload": {"rows": []}}])
        event = notification(old, heads(self.db), self.db.shops())
        self.assertFalse(event["reset"])
        self.assertEqual(
            event["changes"], [{"kind": "tickets", "shop_id": self.db.shops()[0]["id"], "platform": "pdd"}]
        )

    def test_old_progress_exposes_refresh_error_and_recovers(self):
        scope = "response:" + hashlib.sha256(b"home/progress?").hexdigest()
        value = {
            "rows": [{"shop_id": 1, "total": 1, "updated": 1, "overdue_modules": 1, "modules": []}],
            "total_modules": 1,
        }
        self.db.ingest(
            self.node, [{"shop": "0", "scope": scope, "updated": time.time() - 600, "payload": {"value": value}}]
        )
        app = create_app(self.db, "a" * 40)
        headers = {"Authorization": "Bearer " + "a" * 40}
        with TestClient(app) as client:
            app.state.hub.peers[self.node] = object()
            with patch.object(app.state.hub, "request", AsyncMock(side_effect=HTTPException(504, "节点进度读取超时"))):
                first = client.get("/api/home/progress", headers=headers).json()
                self.assertTrue(first["progress_freshness"][0]["stale"])
                client.portal.call(asyncio.sleep, 0.02)
                failed = client.get("/api/home/progress", headers=headers).json()["progress_freshness"][0]
                self.assertGreaterEqual(failed["age_seconds"], 600)
                self.assertEqual(failed["error"], "节点进度读取超时")
                client.portal.call(asyncio.sleep, 0.02)
            with patch.object(
                app.state.hub, "request", AsyncMock(return_value={"status": 200, "content": json.dumps(value).encode()})
            ):
                client.get("/api/home/progress", headers=headers)
                client.portal.call(asyncio.sleep, 0.02)
                recovered = client.get("/api/home/progress", headers=headers).json()["progress_freshness"][0]
                self.assertFalse(recovered["stale"])
                self.assertFalse(recovered["error"])
            app.state.hub.peers.clear()

    def test_node_frequency_controls_source_freshness_and_unknown_is_explicit(self):
        config = {
            "enabled": True,
            "frequency_version": 1,
            "frequency_groups": [{"id": "custom", "name": "custom", "seconds": 80, "timeout": 40, "time": ""}],
            "task_modes": {},
        }
        self.db.ingest(
            self.node, [{"shop": "0", "scope": "collector_status", "updated": 2, "payload": {"collection": config}}]
        )
        actual = configurations(self.db)[self.node]
        self.assertFalse(describe("overview", {}, {}, 921, actual, "pdd", 1000)["overdue"])
        self.assertTrue(describe("overview", {}, {}, 919, actual, "pdd", 1000)["overdue"])
        self.assertEqual(describe("overview", {}, {}, 919, actual, "pdd", 1000)["interval_seconds"], 80)
        self.assertEqual(describe("overview", {}, {}, 919, None, "pdd", 1000)["freshness"], "unknown")
        self.assertEqual(
            describe("overview", {}, {}, 919, {**actual, "enabled": False}, "pdd", 1000)["freshness"], "paused"
        )

    def test_runtime_policy_read_is_public_only(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            with store.connect() as db:
                db.execute(
                    "INSERT OR REPLACE INTO settings VALUES('collection_policy_v3',?)",
                    (json.dumps({"enabled": True, "frequency_version": 1}),),
                )
                db.execute("INSERT OR REPLACE INTO settings VALUES('unrelated_secret',?)", ("secret",))
            self.assertEqual(read_policy(Path(folder)), {"enabled": True, "frequency_version": 1})

    def test_online_old_business_is_stale_and_receipt_time_is_preserved(self):
        config = {
            "enabled": True,
            "frequency_version": 1,
            "frequency_groups": [{"id": "custom", "name": "custom", "seconds": 80, "timeout": 40, "time": ""}],
            "task_modes": {},
        }
        self.db.ingest(
            self.node,
            [{"shop": "0", "scope": "collector_status", "updated": time.time(), "payload": {"collection": config}}],
        )
        service = CentralBusiness(self.db)
        shop = self.db.shops()[0]
        options = options_for("overview")
        key = service.key("overview", options, shop)
        old = time.time() - 600
        record = {"shop": "1", "scope": key, "updated": old, "payload": {"complete": True, "metrics": {"sales": 1}}}
        self.db.ingest(self.node, [record])
        source = service.data("overview", options)["sources"][0]
        self.assertTrue(source["stale"])
        self.assertTrue(source["overdue"])
        self.assertGreaterEqual(source["age_seconds"], 600)
        self.assertNotEqual(source["center_received_at"], source["updated_at"])
        self.db.ingest(self.node, [record])
        again = service.data("overview", options)["sources"][0]
        self.assertEqual(again["center_received_at"], source["center_received_at"])

    def test_heartbeat_does_not_invalidate_business_response_cache(self):
        with TestClient(create_app(self.db, "a" * 40)) as client:
            h = {"Authorization": "Bearer " + "a" * 40}
            first = client.get("/api/attention?summary=true", headers=h)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.headers["X-Query-Cache"], "miss")
            self.db.ingest(
                self.node,
                [{"shop": "0", "scope": "collector_status", "updated": time.time(), "payload": {"version": "test"}}],
            )
            second = client.get("/api/attention?summary=true", headers=h)
            self.assertEqual(second.headers["X-Query-Cache"], "hit")
