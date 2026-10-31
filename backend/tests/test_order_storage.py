import json, tempfile, time, unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from pathlib import Path
from datetime import datetime, timedelta
from backend import order_plan, order_storage, table_index, collection
from backend.store import Store
from backend.center.order_delivery import OrderDelivery
from backend.center.replication_worker import Delivery
from backend.center.database import Database, order_rows, settings
from backend.center.order_api import Changes, ingest, expand, receipt_key
from backend.center.business import load
from sqlalchemy import select, delete


class OrderStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("fixture", "pw")
        self.shop = self.store.list()[0]
        self.day = datetime.now(order_plan.BEIJING).date().isoformat()
        self.scope = order_plan.day_scope(self.day, "recent")
        self.key = order_plan.key(self.shop)

    def tearDown(self):
        self.temp.cleanup()

    def row(self, rid="one", hour="10", status="已成团，待发货"):
        return {"id": rid, "created_at": self.day + " " + hour + ":00:00", "status": status, "amount": 10}

    def publish(self, rows, stamp=10, partial=False, scope=None):
        order_storage.publish(
            self.store,
            self.shop,
            scope or self.scope,
            {"rows": rows, "complete": True, "_sync": {"snapshot_started_at": stamp}},
            partial,
        )

    def test_unchanged_rows_do_not_upload_and_partial_rows_are_visible(self):
        self.publish([self.row()], partial=True)
        worker = OrderDelivery(Path(self.temp.name))
        first = worker.batch()
        self.assertEqual(len(first["rows"]), 1)
        worker.ack = first["rows"][-1]["revision"]
        self.publish([self.row()], 20, True)
        self.assertEqual(worker.batch()["rows"], [])
        saved = self.store.read_cache(self.sid, self.key)["payload"]
        self.assertNotIn(self.day, saved["_order_days"])
        self.assertEqual(table_index.query(self.store, [(self.shop, self.key)], "orders")["total"], 1)
        self.publish([{**self.row(), "amount": 12}], 30, True)
        self.assertEqual(worker.batch()["rows"][0]["payload"]["amount"], 12)

    def test_hour_completion_does_not_delete_other_hours_or_claim_full_day(self):
        self.publish([self.row(), self.row("later", "18")], 10)
        start = self.scope["start"] + 10 * 3600
        scope = {**self.scope, "start": start, "end": start + 3599, "_order_lane": "recheck"}
        self.publish([], 20, scope=scope)
        saved = self.store.read_cache(self.sid, self.key)["payload"]
        self.assertEqual([r["id"] for r in saved["rows"]], ["later"])
        self.assertEqual(saved["_order_days"][self.day], 10)
        self.publish([self.row()], 15, True)
        self.assertEqual(len(self.store.read_cache(self.sid, self.key)["payload"]["rows"]), 1)

    def test_rollback_snapshot_preserved_and_projection_can_rebuild(self):
        legacy = "orders:view-v1:0"
        old = {"rows": [self.row("old")], "_order_days": {self.day: 5}, "complete": False}
        self.store.write_cache(self.sid, legacy, old)
        self.publish([self.row("new")], 10, True)
        self.assertEqual(self.store.read_cache(self.sid, legacy)["payload"], old)
        with self.store.connect() as db:
            db.execute("DELETE FROM table_snapshots WHERE cache_key=?", (self.key,))
            db.execute("DELETE FROM table_rows WHERE cache_key=?", (self.key,))
        table_index.initialize(self.store)
        self.publish([{**self.row("new"), "amount": 20}], 20, True)
        result = table_index.query(self.store, [(self.shop, self.key)], "orders")
        self.assertEqual(result["total"], 2)
        self.assertEqual(next(r["amount"] for r in result["rows"] if r["id"] == "new"), 20)

    def test_open_scan_uses_known_hours_after_daily_discovery(self):
        older = (datetime.now(order_plan.BEIJING).date() - timedelta(days=10)).isoformat()
        scope = next(o for o in order_plan.live() if o.get("_order_lane") == "open" and o["status"] == "待发货")
        row = {**self.row(), "created_at": older + " 10:22:30"}
        self.publish([row], 10, scope=scope)
        self.assertIsNone(order_storage.open_windows(self.store, self.shop, scope))
        mark = f"order-open-scan:{self.sid}:{self.key}:待发货"
        with self.store.connect() as db:
            db.execute("INSERT INTO settings VALUES(?,?)", (mark, str(time.time())))
        windows = order_storage.open_windows(self.store, self.shop, scope)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0][1] - windows[0][0], 3599)
        self.publish([], 20, scope=scope)
        recheck = next(order_plan.background(self.store, self.shop))
        self.assertEqual(recheck["_order_lane"], "recheck")
        self.assertEqual(recheck["end"] - recheck["start"], 3599)
        self.publish([{**row, "id": "unknown", "created_at": ""}], 30, True, scope)
        self.assertIsNone(order_storage.open_windows(self.store, self.shop, scope))

    def test_segment_snapshots_not_uploaded_twice(self):
        self.publish([self.row()], 10)
        self.store.write_cache(
            self.sid, "orders:segment", {"rows": [self.row()], "complete": True, "_sync": {"scope": self.scope}}
        )
        self.store.write_cache(self.sid, "orders:refund", {"rows": [], "complete": True})
        scopes = [r["scope"] for r in Delivery(Path(self.temp.name), "business", "test").collect()]
        self.assertIn(self.key, scopes)
        self.assertIn("orders:refund", scopes)
        self.assertNotIn("orders:segment", scopes)

    def test_local_restore_rotates_stream_and_center_restore_can_resend(self):
        self.publish([self.row()], 10)
        worker = OrderDelivery(Path(self.temp.name))
        batch = worker.batch()
        old = batch["stream"]
        worker.ack = 100
        worker.restore_stream()
        new = worker.batch()
        self.assertNotEqual(new["stream"], old)
        self.assertEqual(len(new["rows"]), 1)
        worker.ack = new["rows"][0]["revision"]
        self.assertEqual(worker.batch()["rows"], [])
        worker.ack = 0
        self.assertEqual(len(worker.batch()["rows"]), 1)


class OrderCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("fixture", "pw")
        self.shop = self.store.list()[0]
        self.db = Database("sqlite:///" + str(Path(self.temp.name) / "center.db"))
        self.node = self.db.register("source")["id"]
        self.worker = OrderDelivery(Path(self.temp.name))
        self.scope = order_plan.day_scope(datetime.now(order_plan.BEIJING).date().isoformat(), "recent")
        self.key = order_plan.key(self.shop)

    def tearDown(self):
        self.db.engine.dispose()
        self.temp.cleanup()

    def batch(self, rows, stamp):
        order_storage.publish(
            self.store, self.shop, self.scope, {"rows": rows, "complete": True, "_sync": {"snapshot_started_at": stamp}}
        )
        return Changes(**self.worker.batch())

    def test_retry_is_idempotent_and_tombstone_reaches_center(self):
        row = {"id": "one", "status": "已成团，待发货", "created_at": self.scope["date_from"] + " 10:00:00"}
        batch = self.batch([row], 10)
        ack = ingest(self.db, self.node, batch)
        self.assertEqual(ingest(self.db, self.node, batch), ack)
        self.worker.ack = ack
        with self.db.engine.connect() as db:
            self.assertEqual(len(db.execute(select(order_rows)).all()), 1)
        deleted = self.batch([], 20)
        self.assertIsNone(deleted.rows[0].payload)
        ingest(self.db, self.node, deleted)
        ingest(self.db, self.node, batch)
        with self.db.engine.connect() as db:
            self.assertIsNone(db.execute(select(order_rows.c.payload)).scalar())

    def test_bad_batch_rolls_back_rows_and_receipt(self):
        batch = self.batch([{"id": "one", "created_at": self.scope["date_from"] + " 10:00:00"}], 10)
        batch.rows[0].payload["id"] = "different"
        with self.assertRaises(ValueError):
            ingest(self.db, self.node, batch)
        with self.db.engine.connect() as db:
            self.assertEqual(db.execute(select(order_rows)).all(), [])
            self.assertIsNone(
                db.execute(
                    select(settings.c.value).where(settings.c.key == receipt_key(self.node, batch.stream))
                ).scalar()
            )

    def test_center_read_and_node_transfer_preserve_incremental_orders(self):
        row = {"id": "one", "status": "已成团，待发货", "created_at": self.scope["date_from"] + " 10:00:00"}
        batch = self.batch([row], 10)
        ingest(self.db, self.node, batch)
        with self.store.connect() as db:
            r = db.execute("SELECT payload,updated FROM business_cache WHERE cache_key=?", (self.key,)).fetchone()
        self.db.ingest(
            self.node,
            [
                {"shop": str(self.sid), "scope": self.key, "payload": json.loads(r[0]), "updated": r[1]},
                {
                    "shop": str(self.sid),
                    "scope": "shop_metadata",
                    "updated": 20,
                    "payload": {"platform": "pdd", "profile_version": 0, "state": "logged_in"},
                },
            ],
        )

        def read(node, shop, key=None):
            return expand(self.db, load(self.db, [(node, str(shop), key or self.key)]))[(node, str(shop))][0]["payload"]

        self.assertEqual(read(self.node, self.sid)["rows"][0]["id"], "one")
        target = self.db.register("target")["id"]
        self.db.move_shop(self.db.shops()[0], target, 9, {"platform": "pdd", "profile_version": 1})
        target_key = "orders:view-v2:1"
        self.assertEqual(self.db.shops()[0]["profile_version"], 1)
        self.assertEqual(read(target, 9, target_key)["rows"][0]["id"], "one")
        late = self.batch([{**row, "status": "已取消"}], 30)
        ingest(self.db, self.node, late)
        self.assertEqual(read(target, 9, target_key)["rows"][0]["status"], "已成团，待发货")

    def test_http_auth_and_partial_business_query(self):
        from fastapi.testclient import TestClient
        from backend.center.server import create_app

        # Own token for API verification; no deployment credentials used by tests.
        node = self.db.register("api")
        headers = {"Authorization": "Bearer " + node["token"], "X-Node-ID": node["id"]}
        batch = self.batch(
            [{"id": "one", "status": "已成团，待发货", "created_at": self.scope["date_from"] + " 10:00:00"}], 10
        )
        with TestClient(create_app(self.db, "a" * 40)) as client:
            self.assertEqual(client.post("/agent/order-changes", json=batch.model_dump()).status_code, 401)
            result = client.post("/agent/order-changes", headers=headers, json=batch.model_dump())
            self.assertEqual(result.status_code, 200, result.text)
            with self.store.connect() as db:
                r = db.execute("SELECT payload,updated FROM business_cache WHERE cache_key=?", (self.key,)).fetchone()
            self.db.ingest(
                node["id"],
                [
                    {"shop": "1", "scope": self.key, "payload": json.loads(r[0]), "updated": r[1]},
                    {
                        "shop": "1",
                        "scope": "shop_metadata",
                        "updated": 20,
                        "payload": {"platform": "pdd", "state": "logged_in", "name": "fixture"},
                    },
                ],
            )
            response = client.get("/api/data/orders?status=待发货", headers={"Authorization": "Bearer " + "a" * 40})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["total"], 1)
            self.assertEqual(response.json()["rows"][0]["id"], "one")


class OrderAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_upload_does_not_advance_ack(self):
        with tempfile.TemporaryDirectory() as folder:
            worker = OrderDelivery(Path(folder))
            worker.summary["pending"] = 1
            batch = {"stream": "a" * 32, "rows": [{"revision": 7}]}
            client = SimpleNamespace(post=AsyncMock(side_effect=TimeoutError("offline")))
            with self.assertRaises(TimeoutError):
                await worker.send(client, batch)
            self.assertEqual(worker.ack, 0)
            client.post = AsyncMock(
                return_value=SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"revision": 7})
            )
            await worker.send(client, batch)
            self.assertEqual(worker.ack, 7)

    async def test_page_is_saved_before_batch_yields_and_resumes(self):
        from backend import orders, collection_checkpoint as cp

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("fixture", "pw")
            shop = store.list()[0]
            day = (datetime.now(order_plan.BEIJING).date() - timedelta(days=2)).isoformat()
            scope = order_plan.day_scope(day, "history")

            async def search(body):
                n = body["pageNumber"]
                return {
                    "success": True,
                    "result": {
                        "pageItems": [
                            {
                                "order_sn": str(n),
                                "order_status_str": "已成团，待发货",
                                "order_time": scope["start"] + 100 * n,
                            }
                        ],
                        "totalItemNum": 2,
                    },
                }

            page = SimpleNamespace(search=search)
            with cp.context(store, sid, "orders:test", {**collection.DEFAULTS, "batch_pages": 1}):
                with self.assertRaises(cp.YieldCollection):
                    await orders.collect(page, scope)
            saved = store.read_cache(sid, order_plan.key(shop))["payload"]
            self.assertEqual(len(saved["rows"]), 1)
            self.assertNotIn(day, saved["_order_days"])
            with cp.context(store, sid, "orders:test", {**collection.DEFAULTS, "batch_pages": 12}):
                result = await orders.collect(page, scope)
            self.assertTrue(result["complete"])
            self.assertEqual(len(result["rows"]), 2)
            order_plan.publish(store, shop, scope, result)
            self.assertIn(day, store.read_cache(sid, order_plan.key(shop))["payload"]["_order_days"])

    async def test_dense_range_splits_before_pagination(self):
        from backend import orders

        seen = []

        async def search(body):
            seen.append((body["groupStartTime"], body["groupEndTime"]))
            return {
                "success": True,
                "result": {
                    "pageItems": [],
                    "totalItemNum": 501 if body["groupEndTime"] - body["groupStartTime"] >= 3600 else 0,
                },
            }

        scope = {"start": 100, "end": 8100, "status": ""}
        result = await orders.collect(SimpleNamespace(search=search), scope)
        self.assertTrue(result["complete"])
        self.assertGreater(len(seen), 1)
        small = [r for r in seen if r[1] - r[0] < 3600]
        self.assertEqual(small[0][0], 100)
        self.assertEqual(small[-1][1], 8100)
        self.assertTrue(all(a[1] + 1 == b[0] for a, b in zip(small, small[1:])))
