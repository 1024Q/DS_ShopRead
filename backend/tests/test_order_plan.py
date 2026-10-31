import tempfile, time, unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from backend import order_plan, collection, collection_plan
from backend.business import BusinessService, options_for, filter_rows
from backend.store import Store
from backend.collection_queue import select_jobs


class OrderPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("test", "pw")
        self.shop = self.store.list()[0]
        self.today = datetime.now(order_plan.BEIJING).date()

    def tearDown(self):
        self.temp.cleanup()

    def publish(self, options, rows, stamp=None):
        order_plan.publish(
            self.store,
            self.shop,
            options,
            {"rows": rows, "complete": True, "_sync": {"snapshot_started_at": stamp or time.time()}},
        )

    def test_hot_reads_only_two_days_or_open_status(self):
        hot = list(order_plan.live())
        broad = [o for o in hot if o["end"] - o["start"] > 86400]
        self.assertTrue(all(o["status"] in (*order_plan.OPEN, "即将逾期发货") for o in broad))
        self.assertEqual(len([o for o in hot if o.get("_order_lane") == "recent"]), 2)
        from backend.tests.collection_fixture import assign

        config = assign(
            self.store, {"orders@today": "custom", "orders@unpaid": "custom", "orders@shipping_due": "custom"}
        )
        self.assertEqual(collection.effective_interval("orders", hot[0], config, "pdd"), 300)

    def test_recent_and_open_partition_cover_every_day_without_overlap(self):
        hot = list(order_plan.live())
        recent = [o for o in hot if o.get("_order_lane") == "recent"]
        older = next(o for o in hot if o.get("_order_lane") == "open")
        self.assertEqual(older["end"] + 1, min(o["start"] for o in recent))
        self.assertFalse(collection_plan.obsolete_realtime("orders", older))
        self.assertTrue(collection_plan.obsolete_realtime("orders", {**older, "_order_partition": 1}))
        self.assertEqual((self.today - datetime.fromisoformat(older["date_from"]).date()).days, 89)

    def test_older_status_read_cannot_clear_recent_open_orders(self):
        today = order_plan.day_scope(self.today.isoformat(), "recent")
        self.publish(
            today, [{"id": "recent", "created_at": today["date_from"] + " 10:00:00", "status": "已发货，待收货"}], 10
        )
        older = next(o for o in order_plan.live() if o.get("_order_lane") == "open" and o["status"] == "待收货")
        self.publish(older, [], 20)
        saved = self.store.read_cache(self.sid, order_plan.key(self.shop))["payload"]
        self.assertEqual(saved["rows"][0]["attention_status"], "待收货")
        self.assertEqual(saved["_order_recheck_days"], [])

    def test_completed_day_visible_before_history_complete(self):
        scope = order_plan.day_scope(self.today.isoformat(), "recent")
        self.publish(scope, [{"id": "1", "created_at": scope["date_from"] + " 10:00:00", "status": "已成团，待发货"}])
        saved = self.store.read_cache(self.sid, order_plan.key(self.shop))["payload"]
        self.assertEqual(len(saved["rows"]), 1)
        self.assertFalse(saved["complete"])
        self.assertEqual(len(filter_rows(saved["rows"], "orders", status="待发货")), 1)
        next_read = list(order_plan.background(self.store, self.shop))
        self.assertEqual(next_read, [])

    def test_disappeared_open_order_rechecked_without_inventing_final_status(self):
        day = (self.today - timedelta(days=10)).isoformat()
        op = next(o for o in order_plan.live() if o.get("_order_lane") == "open" and o["status"] == "待发货")
        self.publish(op, [{"id": "x", "created_at": day + " 00:00:00", "status": "已成团，待发货"}], 10)
        self.publish(op, [], 20)
        saved = self.store.read_cache(self.sid, order_plan.key(self.shop))["payload"]
        self.assertFalse(filter_rows(saved["rows"], "orders", status="待发货"))
        self.assertIn(day, saved["_order_recheck_days"])
        self.publish(
            order_plan.day_scope(day, "recheck"),
            [{"id": "x", "created_at": day + " 00:00:00", "status": "已发货，待收货"}],
            30,
        )
        saved = self.store.read_cache(self.sid, order_plan.key(self.shop))["payload"]
        self.assertEqual(len(filter_rows(saved["rows"], "orders", status="待收货")), 1)
        self.assertNotIn(day, saved["_order_recheck_days"])
        self.publish(op, [{"id": "x", "created_at": day + " 00:00:00", "status": "已成团，待发货"}], 15)
        self.assertEqual(
            self.store.read_cache(self.sid, order_plan.key(self.shop))["payload"]["rows"][0]["status"], "已发货，待收货"
        )

    def test_queries_share_materialized_view_not_collector_scope(self):
        service = BusinessService(self.store, lambda _: SimpleNamespace(state="logged_in"))
        self.assertEqual(service.key("orders", options_for("orders"), self.shop), order_plan.key(self.shop))
        self.assertNotEqual(service.key("orders", next(order_plan.live()), self.shop), order_plan.key(self.shop))

    def test_request_never_enqueues_full_history_and_seeds_old_completed_rows(self):
        options = options_for("orders")
        calls = []
        service = SimpleNamespace(
            store=self.store,
            key=lambda k, o, s: BusinessService.key(None, k, o, s),
            request_sync=lambda k, o, *a, **kw: calls.append(o) or {"tasks": []},
        )
        order_plan.request(service, {**options, "status": "待发货"}, self.sid, False, "pdd")
        self.assertTrue(calls)
        self.assertFalse(any(not o.get("status") and o["end"] - o["start"] > 86400 for o in calls))
        legacy = BusinessService.key(None, "orders", options, self.shop, legacy=True)
        self.store.write_cache(
            self.sid,
            legacy,
            {
                "rows": [{"id": "old", "created_at": options["date_from"] + " 00:00:00", "status": "交易成功"}],
                "complete": True,
                "_sync": {"scope": options},
            },
        )
        self.publish(next(order_plan.live()), [])
        saved = self.store.read_cache(self.sid, order_plan.key(self.shop))["payload"]
        self.assertIn("old", [r["id"] for r in saved["rows"]])
        self.assertTrue(saved["complete"])

    def test_due_age_is_not_overridden_by_retired_history_priority(self):
        now = time.time()
        config = {**collection.DEFAULTS, "max_concurrent": 1, "realtime_concurrent": 0, "daily_concurrent": 1}
        history = {"kind": "orders", "phase": "queued", "queued_at": now - 900, "scope": {"_order_lane": "history"}}
        recent = {"kind": "orders", "phase": "queued", "queued_at": now, "scope": {"_order_lane": "recent"}}
        self.assertEqual(select_jobs({(1, "old"): history, (2, "new"): recent}, config, now=now), [(1, "old")])

    def test_old_open_and_recent_scopes_expire_history_does_not(self):
        old = (self.today - timedelta(days=5)).isoformat()
        self.assertTrue(collection_plan.obsolete_realtime("orders", order_plan.day_scope(old, "recent")))
        self.assertFalse(collection_plan.obsolete_realtime("orders", order_plan.day_scope(old, "history")))
