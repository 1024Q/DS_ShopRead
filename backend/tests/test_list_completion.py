import copy
import json
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import update

from backend import collection, list_completion as local
from backend.business import BusinessService, create_router, options_for
from backend.collection_plan import roll_window
from backend.store import Store


def body(kind, shops):
    return {"request_id": str(uuid.uuid4()), "shop_ids": shops, "status": local.ALLOWED[kind]}


def complete_payload(scope, request_id, complete=True):
    return {
        "complete": complete,
        "rows": [
            {
                "id": "pending",
                "status": scope["status"],
                "attention_status": scope["status"],
                "name": "商品",
                "created_at": scope["date_to"] + " 12:00:00",
            }
        ],
        "_sync": {"scope": scope, "snapshot_started_at": time.time()},
        "_list_coverage": {
            "version": 1,
            "request_id": request_id,
            "status": scope["status"],
            "date_from": scope["date_from"],
            "date_to": scope["date_to"],
            "snapshot_started_at": time.time(),
        },
    }


class LocalCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("fixture", "pw", name="测试店铺")
        self.shop = self.store.list()[0]
        self.service = BusinessService(self.store, lambda _: SimpleNamespace(state="logged_in"))
        self.service.export_jobs = Mock()
        self.config = copy.deepcopy(collection.settings(self.store))
        self.config["task_modes"].setdefault("pdd", {}).update({"orders@unfinished": "off", "aftersales@work": "off"})
        with self.store.write_connection() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('collection_policy_v3',?)", (json.dumps(self.config),))
        self.before = collection.settings(self.store)

    def records(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM collection_tasks WHERE shop_id=?", (self.sid,))]

    def test_concurrent_duplicate_creation_is_atomic_and_does_not_change_settings(self):
        # Exercise the per-shop path used by production, not only a monolithic DB.
        self.store.enable_shop_databases()
        request = body("orders", [self.sid])
        with ThreadPoolExecutor(max_workers=4) as pool:
            result = list(pool.map(lambda _: local.create(self.service, "orders", request), range(8)))
        records = self.records()
        self.assertEqual(len(records), 1)
        scope = json.loads(records[0]["options"])
        self.assertEqual({r["tasks"][0]["created_at"] for r in result}, {scope[local.MARKER]["created_at"]})
        self.assertEqual(collection.settings(self.store), self.before)
        self.assertEqual(collection.mode("orders", options_for("orders", status="待发货"), self.before), "off")
        self.assertNotEqual(collection.mode("orders", scope, self.before), "off")

    def test_frozen_range_survives_retry_restart_and_success_retires_only_once_task(self):
        from backend.collection_checkpoint import Checkpoint
        from backend.task_lifecycle import before_run, reconcile, retained

        for kind in local.ALLOWED:
            request = body(kind, [self.sid])
            local.create(self.service, kind, request)
            record = next(r for r in self.records() if r["kind"] == kind)
            scope = json.loads(record["options"])
            key = record["cache_key"]
            self.assertNotIn("_relative_days", scope)
            self.assertEqual(roll_window(scope), scope)
            checkpoint = Checkpoint(self.store, self.sid, key, scope)
            checkpoint.save()
            collection.finish(self.store, self.sid, key, error="临时网络失败", error_kind="network")
            retained_rows = reconcile(
                self.store, self.shop, set(), self.service.key, set(), now=time.time() + 2 * 86400
            )
            self.assertIn(key, [r["cache_key"] for r in retained_rows])
            restarted = BusinessService(self.store, self.service.session_getter)
            local.create(restarted, kind, request)
            self.assertEqual(json.loads(collection.state(self.store, self.sid, key)["options"]), scope)
            self.assertTrue(before_run(self.store, self.shop, kind, scope, key, self.service.key, config=self.before))
            collection.finish(self.store, self.sid, key)
            self.assertFalse(retained(collection.state(self.store, self.sid, key), scope))
            self.assertFalse(before_run(self.store, self.shop, kind, scope, key, self.service.key, config=self.before))
        self.assertEqual(collection.settings(self.store), self.before)

    def test_partial_cannot_publish_complete_status_and_order_proof_needs_no_all_day_history(self):
        kind = "orders"
        request = body(kind, [self.sid])
        local.create(self.service, kind, request)
        record = self.records()[0]
        scope = json.loads(record["options"])
        payload = complete_payload(scope, request["request_id"], complete=False)
        with self.assertRaisesRegex(ValueError, "尚未读取完整"):
            local.publish(self.service, self.shop, kind, scope, payload)
        self.assertIsNone(self.store.read_cache(self.sid, local.order_key(self.shop)))
        payload["complete"] = True
        local.publish(self.service, self.shop, kind, scope, payload)
        saved = self.store.read_cache(self.sid, local.order_key(self.shop))["payload"]
        self.assertTrue(local.order_candidate(saved, options_for(kind, status="待发货")))
        self.assertNotIn("_order_days", saved)
        self.assertFalse(local.order_candidate(saved, options_for(kind, status="待收货")))
        self.assertFalse(
            local.order_candidate(saved, {**options_for(kind, status="待发货"), "date_from": "2000-01-01"})
        )
        app = FastAPI()
        app.include_router(create_router(self.service, lambda: None))
        with TestClient(app) as client:
            query = {"status": "待发货", "shop_id": self.sid}
            rows = client.get("/api/data/orders", params=query).json()
            counts = client.get("/api/data/orders", params={**query, "home_counts": True}).json()
        self.assertEqual(rows["total"], 1)
        self.assertEqual(counts["total"], rows["total"])
        self.assertTrue(counts["home_counts"]["rows"][0]["verified"])
        self.assertEqual(counts["home_counts"]["rows"][0]["freshness_status"], "paused")

    def test_routes_are_authenticated_validate_scope_and_do_not_accept_internal_marker(self):
        app = FastAPI()

        def denied():
            raise HTTPException(401)

        app.include_router(create_router(self.service, denied))
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/data/orders/complete-once").status_code, 401)
            self.assertEqual(
                client.post("/api/data/orders/complete-once", json=body("orders", [self.sid])).status_code, 401
            )
        app = FastAPI()
        app.include_router(create_router(self.service, lambda: None))
        with TestClient(app) as client:
            for bad in ({"request_id": "invalid"}, {"status": "待收货"}, {"shop_ids": [999999]}):
                response = client.post("/api/data/orders/complete-once", json={**body("orders", [self.sid]), **bad})
                self.assertEqual(response.status_code, 400, response.text)
            response = client.post(
                "/api/data/orders/complete-once", json={**body("orders", [self.sid]), local.MARKER: {}}
            )
            self.assertEqual(response.status_code, 422)
            with patch.object(self.service, "request_sync", return_value={"tasks": []}) as ordinary:
                client.post(
                    "/api/data/orders/sync", params={"status": "待发货", local.MARKER: json.dumps({"version": 1})}
                )
                self.assertNotIn(local.MARKER, ordinary.call_args.args[1])
        self.assertEqual(self.records(), [])

    def test_completed_counts_use_matching_snapshot_after_restart_and_newer_repair(self):
        for kind in local.ALLOWED:
            with self.subTest(kind=kind):
                request = body(kind, [self.sid])
                local.create(self.service, kind, request)
                record = next(r for r in self.records() if r["kind"] == kind)
                scope = json.loads(record["options"])
                key = record["cache_key"]
                payload = complete_payload(scope, request["request_id"])
                payload["source_total"] = 2
                # Normal collection saves the request copy before the readable list.
                self.store.write_cache(self.sid, key, payload)
                local.publish(self.service, self.shop, kind, scope, payload)
                collection.finish(self.store, self.sid, key)
                self.service.progress[(self.sid, key)] = {"loaded": 999, "total": 999}
                completed = local.listing(self.service, kind, request["request_id"])["tasks"][0]
                self.assertEqual((completed["loaded"], completed["total"]), (1, 2))
                newer = complete_payload(scope, str(uuid.uuid4()))
                newer["rows"] = []
                self.store.write_cache(self.sid, completed["result_key"], newer)
                restarted = BusinessService(self.store, self.service.session_getter)
                with patch.object(
                    self.store, "read_cache", side_effect=AssertionError("must not hydrate business rows")
                ):
                    saved = local.listing(restarted, kind, request["request_id"])["tasks"][0]
                self.assertEqual((saved["loaded"], saved["total"]), (1, 2))
                self.assertEqual(saved["completed_at"], completed["completed_at"])

    def test_completed_empty_list_is_zero_and_unmatched_snapshot_is_unknown(self):
        request = body("orders", [self.sid])
        local.create(self.service, "orders", request)
        record = self.records()[0]
        scope = json.loads(record["options"])
        payload = complete_payload(scope, request["request_id"])
        payload["rows"] = []
        local.publish(self.service, self.shop, "orders", scope, payload)
        collection.finish(self.store, self.sid, record["cache_key"])
        saved = local.listing(self.service, "orders", request["request_id"])["tasks"][0]
        self.assertEqual((saved["loaded"], saved["total"]), (0, 0))
        self.store.write_cache(self.sid, saved["result_key"], complete_payload(scope, str(uuid.uuid4())))
        missing = local.listing(self.service, "orders", request["request_id"])["tasks"][0]
        self.assertEqual((missing["loaded"], missing["total"]), (None, None))
        self.assertEqual(missing["status"], "uploading")

    def test_live_progress_is_bounded_throttled_and_writes_only_changes(self):
        for _ in range(4):
            local.create(self.service, "orders", body("orders", [self.sid]))
        records = self.records()
        restarted = BusinessService(self.store, self.service.session_getter)
        for record in records:
            restarted.progress[(self.sid, record["cache_key"])] = {
                "phase": "running",
                "stage": "读取分页",
                "loaded": 10,
                "total": 20,
            }
        with patch.object(self.store, "write_cache", wraps=self.store.write_cache) as write:
            self.assertEqual(local.publish_live_statuses(restarted, self.shop, records, now=100), 3)
            self.assertEqual(write.call_count, 3)
            self.assertEqual(local.publish_live_statuses(restarted, self.shop, records, now=100), 1)
            for progress in restarted.progress.values():
                progress["loaded"] = 15
            self.assertEqual(local.publish_live_statuses(restarted, self.shop, records, now=109.9), 0)
            self.assertEqual(local.publish_live_statuses(restarted, self.shop, records, now=110), 3)
            self.assertEqual(local.publish_live_statuses(restarted, self.shop, records, now=110), 1)
            write.reset_mock()
            self.assertEqual(local.publish_live_statuses(restarted, self.shop, records, now=120), 0)
            write.assert_not_called()
            # Only a changed waiting state needs a new receipt after progress stops.
            first = records[0]
            restarted.progress.pop((self.sid, first["cache_key"]))
            waiting = {**first, "failures": 1, "retry_at": 456, "last_error": "临时网络失败"}
            self.assertEqual(local.publish_live_statuses(restarted, self.shop, [waiting], now=120), 1)
            receipt = write.call_args.args[2]
            self.assertEqual((receipt["status"], receipt["retry_at"]), ("retry", 456))
        self.assertEqual(collection.settings(self.store), self.before)

    def test_final_receipt_fences_stale_live_scan_and_is_not_polled_after_restart(self):
        request = body("orders", [self.sid])
        local.create(self.service, "orders", request)
        stale = self.records()[0]
        scope = json.loads(stale["options"])
        stale_value = local.status(self.service, self.shop, stale)
        local.publish(self.service, self.shop, "orders", scope, complete_payload(scope, request["request_id"]))
        collection.finish(self.store, self.sid, stale["cache_key"])
        with patch.object(self.store, "write_cache", wraps=self.store.write_cache) as write:
            local.publish_status(self.service, self.shop, "orders", scope, stale["cache_key"])
            self.assertEqual(write.call_count, 1)
            self.assertEqual(write.call_args.args[2]["status"], "uploading")
            self.assertFalse(local.write_status(self.service, self.shop, stale, stale_value))
            local.publish_status(self.service, self.shop, "orders", scope, stale["cache_key"])
            self.assertEqual(write.call_count, 1)
            restarted = BusinessService(self.store, self.service.session_getter)
            with patch.object(local, "status", side_effect=AssertionError("completed requests must not be polled")):
                self.assertEqual(local.publish_live_statuses(restarted, self.shop, self.records()), 0)
        self.assertEqual(
            self.store.read_cache(self.sid, local.receipt_key("orders", request["request_id"]))["payload"]["status"],
            "uploading",
        )


class CenterCompletionTests(unittest.TestCase):
    def setUp(self):
        from backend.center.database import Database
        from backend.center.business import CentralBusiness

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database("sqlite:///" + str(Path(self.tmp.name) / "center.db"))
        self.addCleanup(self.db.engine.dispose)
        self.node = self.db.register("测试节点")["id"]
        self.ingest("shop_metadata", {"platform": "pdd", "name": "测试店铺", "state": "sleeping"})
        self.service = CentralBusiness(self.db)
        self.service.export_jobs = Mock()
        self.shop = self.db.shops()[0]
        self.app = FastAPI()
        self.app.include_router(create_router(self.service, lambda: None))
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def ingest(self, scope, payload, shop="7", updated=None):
        self.db.ingest(
            self.node, [{"shop": shop, "scope": scope, "payload": payload, "updated": updated or time.time()}]
        )

    def submit(self, kind="orders"):
        request = body(kind, [self.shop["id"]])
        response = self.client.post("/api/data/" + kind + "/complete-once", json=request)
        self.assertEqual(response.status_code, 202, response.text)
        return request, response.json()["tasks"][0]

    def get(self, request, kind="orders"):
        return self.client.get(
            "/api/data/" + kind + "/complete-once", params={"request_id": request["request_id"]}
        ).json()["tasks"][0]

    def capable(self):
        self.ingest("collector_status", {"capabilities": [local.CAPABILITY]}, shop="0")

    def test_duplicate_job_is_durable_and_waits_for_capability_or_online(self):
        from backend.center.database import jobs, nodes

        request, task = self.submit()
        self.client.post("/api/data/orders/complete-once", json=request)
        self.assertEqual(len(self.db.list(jobs)), 1)
        self.assertEqual(self.get(request)["status"], "waiting_upgrade")
        self.assertIsNone(self.db.claim(self.node))
        with self.db.engine.begin() as db:
            db.execute(update(nodes).where(nodes.c.id == self.node).values(seen=0))
        self.assertEqual(self.get(request)["status"], "waiting_node")
        with patch("backend.center.database.time.time", return_value=time.time() + 10 * 86400):
            self.assertEqual(self.db.purge_jobs(), 0)
        self.capable()
        claimed = self.db.claim(self.node)
        self.assertEqual(claimed["id"], task["job_id"])

    def test_failure_is_retried_without_expiry_and_dispatch_ack_is_not_completion(self):
        from backend.center.database import jobs

        self.capable()
        request, _ = self.submit()
        claimed = self.db.claim(self.node)
        self.assertTrue(
            self.db.finish(self.node, claimed["id"], claimed["lease"], {"ok": False, "message": "节点暂不可用"})
        )
        self.assertIsNone(self.db.claim(self.node))
        self.assertEqual(self.get(request)["status"], "retry")
        self.assertEqual(self.get(request)["last_error"], "节点暂不可用")
        self.assertGreater(self.get(request)["retry_at"], time.time())
        with patch("backend.center.database.time.time", return_value=time.time() + 61):
            claimed = self.db.claim(self.node)
            self.assertTrue(self.db.finish(self.node, claimed["id"], claimed["lease"], {"ok": True}))
        self.assertFalse(self.get(request)["center_confirmed"])
        self.assertEqual(self.db.list(jobs)[0]["state"], "dispatched")

    def test_only_matching_full_business_receipt_confirms_and_confirmation_survives_new_snapshot(self):
        self.capable()
        request, _ = self.submit()
        claimed = self.db.claim(self.node)
        key = local.order_key(self.shop)
        scope = options_for("orders", status="待发货")
        rid = request["request_id"]
        self.ingest(
            local.receipt_key("orders", rid),
            {"completed_at": time.time(), "result_key": key, "status": "uploading", "loaded": 0, "total": None},
        )
        self.assertFalse(self.get(request)["center_confirmed"])
        self.ingest(key, complete_payload(scope, rid, complete=False))
        self.assertFalse(self.get(request)["center_confirmed"])
        self.ingest(key, complete_payload(scope, str(uuid.uuid4())))
        self.assertFalse(self.get(request)["center_confirmed"])
        self.ingest(key, complete_payload(scope, rid))
        self.assertTrue(self.get(request)["center_confirmed"])
        # A late HTTP dispatch reply must not overwrite a fast business receipt.
        self.assertTrue(self.db.finish(self.node, claimed["id"], claimed["lease"], {"ok": True}))
        confirmed = self.get(request)
        self.assertEqual(confirmed["status"], "completed")
        self.assertEqual((confirmed["loaded"], confirmed["total"]), (1, 1))
        self.assertGreater(confirmed["center_received_at"], 0)
        newer, _ = self.submit()
        replacement = complete_payload(scope, newer["request_id"])
        replacement["rows"] = []
        self.ingest(key, replacement)
        self.assertEqual(self.get(request)["status"], "completed")
        self.assertEqual(self.get(newer)["status"], "completed")
        self.assertEqual((self.get(request)["loaded"], self.get(request)["total"]), (1, 1))
        self.assertEqual((self.get(newer)["loaded"], self.get(newer)["total"]), (0, 0))
        query = {"status": "待发货", "shop_id": self.shop["id"]}
        rows = self.client.get("/api/data/orders", params=query).json()
        counts = self.client.get("/api/data/orders", params={**query, "home_counts": True}).json()
        self.assertEqual(rows["total"], counts["total"])
        self.assertTrue(counts["home_counts"]["rows"][0]["verified"])

    def test_legacy_confirmation_counts_are_read_only_and_match_request(self):
        from backend.center.database import jobs

        self.capable()
        request, task = self.submit()
        key = local.order_key(self.shop)
        scope = options_for("orders", status="待发货")
        payload = complete_payload(scope, request["request_id"])
        payload["source_total"] = 3
        self.ingest(key, payload)
        result = self.db.list(jobs)[0]["result"]
        result["list_completion"].pop("loaded")
        result["list_completion"].pop("total")
        with self.db.engine.begin() as db:
            db.execute(update(jobs).where(jobs.c.id == task["job_id"]).values(result=result))
        legacy = self.get(request)
        self.assertEqual((legacy["loaded"], legacy["total"]), (1, 3))
        # A repeated upload must not change an existing completion proof either.
        self.ingest(key, payload)
        self.assertEqual(self.db.list(jobs)[0]["result"], result)
        self.assertEqual(self.get(request)["completed_at"], legacy["completed_at"])
        self.ingest(key, complete_payload(scope, str(uuid.uuid4())))
        unmatched = self.get(request)
        self.assertEqual((unmatched["loaded"], unmatched["total"]), (None, None))
        self.assertTrue(unmatched["center_confirmed"])
        self.assertEqual(self.db.list(jobs)[0]["result"], result)


class CompletionDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_once_command_uses_dedicated_endpoint_not_force_sync(self):
        from backend.center.collection_requests import dispatch

        request = body("tickets", [7])
        transport = Mock(post=AsyncMock(return_value=Mock(raise_for_status=Mock())))
        await dispatch(
            transport,
            {
                "kind": "tickets",
                "shop_id": 7,
                "status": request["status"],
                local.MARKER: {"version": 1, "id": request["request_id"]},
            },
        )
        transport.post.assert_awaited_once_with("/api/data/tickets/complete-once", json=request)


if __name__ == "__main__":
    unittest.main()
