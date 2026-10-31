import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from backend.business import options_for
from backend.center.collection_requests import META, build, dispatch, expired, params
from backend.center.database import Database, jobs


class CollectionIntentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database("sqlite:///" + str(Path(self.temp.name) / "center.db"))
        self.node = self.db.register("采集节点")["id"]

    def tearDown(self):
        self.db.engine.dispose()
        self.temp.cleanup()

    def test_automatic_equivalent_queries_renew_one_queued_demand(self):
        scope = options_for("reviews", stars="2,1")
        first = build("reviews", scope, 7, source="automatic", now=1000)
        second = build("reviews", {**scope, "stars": [1, 2]}, 7, source="automatic", now=1100)
        with patch("backend.center.database.time.time", return_value=1100):
            job = self.db.enqueue(self.node, first)
            self.assertEqual(job, self.db.enqueue(self.node, second))
        saved = self.db.list(jobs)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["request"][META]["valid_until"], 1400)
        self.assertNotIn("date_from", saved[0]["request"])

    def test_expired_automatic_is_not_dispatched_but_manual_and_legacy_are_kept(self):
        automatic = build("overview", options_for("overview"), 7, source="automatic", now=1000)
        manual = build("overview", options_for("overview"), 7, force=True, now=1000)
        legacy = {"kind": "orders", "shop_id": 7, "date_from": "2026-09-01", "date_to": "2026-09-02"}
        with patch("backend.center.database.time.time", return_value=1000):
            old = self.db.enqueue(self.node, automatic)
            manual_id = self.db.enqueue(self.node, manual)
            legacy_id = self.db.enqueue(self.node, legacy)
        with patch("backend.center.database.time.time", return_value=1500):
            first = self.db.claim(self.node)
            second = self.db.claim(self.node)
        self.assertEqual({first["id"], second["id"]}, {manual_id, legacy_id})
        saved = next(r for r in self.db.list(jobs) if r["id"] == old)
        self.assertEqual(saved["state"], "failed")
        self.assertTrue(saved["result"]["expired"])

    def test_active_lease_is_immutable_and_manual_force_not_coalesced_with_auto(self):
        request = build("overview", options_for("overview"), 7, source="automatic", now=1000)
        with patch("backend.center.database.time.time", return_value=1000):
            job = self.db.enqueue(self.node, request)
            claim = self.db.claim(self.node)
            renewed = build("overview", options_for("overview"), 7, source="automatic", now=1050)
            self.assertEqual(job, self.db.enqueue(self.node, renewed))
            other = self.db.enqueue(self.node, build("overview", options_for("overview"), 7, force=True, now=1050))
        self.assertNotEqual(job, other)
        saved = next(r for r in self.db.list(jobs) if r["id"] == job)
        self.assertEqual(saved["request"][META]["generated_at"], 1000)
        self.assertEqual(saved["lease"], claim["lease"])

    def test_expired_auto_lease_is_fenced_without_losing_manual_lease_recovery(self):
        with patch("backend.center.database.time.time", return_value=1000):
            job = self.db.enqueue(
                self.node, build("overview", options_for("overview"), 7, source="automatic", now=1000)
            )
            claim = self.db.claim(self.node)
        with patch("backend.center.database.time.time", return_value=1500):
            self.assertIsNone(self.db.claim(self.node))
            self.assertFalse(self.db.finish(self.node, job, claim["lease"], {"ok": True}))

    def test_explicit_history_and_exact_dates_are_durable(self):
        scope = options_for("reviews", "2026-09-01", "2026-09-02")
        request = build("reviews", {**scope, "_exact_dates": True}, 7, source="automatic", now=1000)
        self.assertEqual(request["date_from"], "2026-09-01")
        self.assertTrue(request["exact_dates"])
        self.assertEqual(request[META]["source"], "historical")
        self.assertFalse(expired(request, 100000))
        period = build("tax", {"period": "2026-Q3"}, 7, source="automatic", now=1000)
        self.assertFalse(expired(period, 100000))

    def test_rolling_order_dates_are_not_reintroduced_as_historical_ranges(self):
        request = build("orders", options_for("orders", status="即将逾期发货"), 7, source="automatic")
        self.assertNotIn("date_from", request)
        self.assertNotIn("date_to", request)
        self.assertEqual(request["status"], "即将逾期发货")
        self.assertEqual(params(request)["force"], "false")
        self.assertNotIn(META, params(request))
        self.assertEqual(params({"kind": "orders", "shop_id": 7})["force"], "true")

    def test_missing_data_creates_automatic_intent_and_sleeping_is_not_login_failure(self):
        from backend.center.business import CentralBusiness

        now = time.time()
        self.db.ingest(
            self.node,
            [
                {
                    "shop": "7",
                    "scope": "shop_metadata",
                    "updated": now,
                    "payload": {"platform": "pdd", "name": "测试店铺", "state": "sleeping"},
                }
            ],
        )
        service = CentralBusiness(self.db)
        scope = options_for("products")
        result = service.data("products", scope)
        self.assertIsNone(result["sources"][0]["error"])
        self.assertEqual(self.db.list(jobs)[0]["request"][META]["source"], "automatic")
        shop = self.db.shops()[0]
        self.db.ingest(
            self.node,
            [
                {
                    "shop": "7",
                    "scope": service.key("products", scope, shop),
                    "updated": now,
                    "payload": {"complete": True, "rows": []},
                }
            ],
        )
        result = service.data("products", scope)
        self.assertFalse(result["sources"][0]["stale"])
        self.assertIsNone(result["sources"][0]["error"])

    def test_missing_get_then_browser_registration_keeps_one_expiring_auto_demand(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.business import create_router
        from backend.center.business import CentralBusiness

        self.db.ingest(
            self.node,
            [
                {
                    "shop": "7",
                    "scope": "shop_metadata",
                    "updated": time.time(),
                    "payload": {"platform": "pdd", "name": "测试店铺", "state": "sleeping"},
                }
            ],
        )
        service = CentralBusiness(self.db)
        service.export_jobs = Mock()
        app = FastAPI()
        app.include_router(create_router(service, lambda: None))
        shop = self.db.shops()[0]
        query = {"shop_id": shop["id"]}
        with TestClient(app) as client:
            response = client.get("/api/data/products", params=query)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIsNone(response.json()["sources"][0]["updated_at"])
            response = client.post("/api/data/products/sync", params={**query, "source": "automatic"})
            self.assertEqual(response.status_code, 202, response.text)
            saved = self.db.list(jobs)
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["request"][META]["source"], "automatic")
            self.assertFalse(saved[0]["request"][META]["force"])
            self.assertGreater(saved[0]["request"][META]["valid_until"], time.time())
            # Older clients retain durable manual semantics; force cannot be
            # accidentally downgraded to an expiring background demand.
            for extra in ({}, {"source": "automatic", "force": True}):
                response = client.post("/api/data/products/sync", params={**query, **extra})
                self.assertEqual(response.status_code, 202, response.text)
        manual = [row["request"][META] for row in self.db.list(jobs) if row["request"][META]["source"] == "manual"]
        self.assertEqual(len(manual), 2)
        self.assertEqual({row["force"] for row in manual}, {False, True})
        self.assertTrue(all(row["valid_until"] == 0 for row in manual))

    def test_local_registration_accepts_auto_intent_without_changing_local_contract(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.business import create_router

        service = SimpleNamespace(request_sync=Mock(return_value={"tasks": []}))
        app = FastAPI()
        app.include_router(create_router(service, lambda: None))
        with TestClient(app) as client:
            response = client.post("/api/data/products/sync?shop_id=7&source=automatic")
            self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(service.request_sync.call_args.args[2:], (7, False))
        self.assertEqual(service.request_sync.call_args.kwargs, {"platform": None})


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_auto_reuses_node_checks_manual_force_is_preserved(self):
        local = Mock(post=AsyncMock(return_value=Mock(raise_for_status=Mock())))
        automatic = build("overview", options_for("overview"), 7, source="automatic")
        result = await dispatch(local, automatic)
        self.assertTrue(result["ok"])
        self.assertEqual(local.post.call_args.kwargs["params"]["force"], "false")
        self.assertNotIn(META, local.post.call_args.kwargs["params"])
        await dispatch(local, build("overview", options_for("overview"), 7, force=True))
        self.assertEqual(local.post.call_args.kwargs["params"]["force"], "true")

    async def test_expired_during_delivery_does_not_start_collector_or_clear_checkpoints(self):
        local = Mock(post=AsyncMock())
        request = build("overview", options_for("overview"), 7, source="automatic", now=1000)
        result = await dispatch(local, request)
        self.assertTrue(result["skipped"])
        local.post.assert_not_called()
