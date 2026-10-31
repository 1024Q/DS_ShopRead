import tempfile, time, unittest
from types import SimpleNamespace
from backend import collection
from backend.business import BusinessService, options_for
from backend.collection_demands import covers, project
from backend.collection_queue import select_jobs, queue_status
from backend.store import Store


class DemandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("fixture", "pw")
        self.shop = self.store.list()[0]
        self.service = BusinessService(self.store, lambda _: None)
        self.d = self.service.demands
        self.broad = options_for("tickets", status="待处理")
        self.small = {**self.broad, "start": self.broad["start"] + 86400 * 170, "_relative_days": 10}

    def tearDown(self):
        self.tmp.cleanup()

    def candidate(self, o, **state):
        return (self.shop, None, "tickets", o, state)

    def test_same_demand_has_multiple_origins_but_one_identity(self):
        rows = self.d.replace(self.shop, [("tickets", self.broad, "常规"), ("tickets", self.broad, "订阅")])
        self.assertEqual(len(rows), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM collection_demands").fetchone()[0], 1)
        self.assertEqual(len(self.d.coalesce([self.candidate(self.broad)] * 2, 100)), 1)

    def test_coverage_requires_same_semantics_and_platform(self):
        self.assertFalse(covers("tickets", self.broad, self.small))
        self.assertTrue(covers('tickets', self.broad, dict(self.broad)))
        self.assertFalse(covers("tickets", self.small, self.broad))
        self.assertFalse(covers("tickets", self.broad, {**self.small, "status": "处理中"}))
        self.assertFalse(covers("tickets", {**self.broad, "status": ""}, {**self.small, "status": "2小时内将逾期"}))
        self.assertFalse(covers("tickets", self.broad, self.small, "taobao"))

    def test_wait_is_bounded_and_urgent_never_waits_for_full_list(self):
        a = self.candidate(self.broad)
        b = self.candidate(self.small)
        self.assertEqual(self.d.coalesce([b, a], 100), [b, a])
        self.assertEqual(len(self.d.merged), 0)
        self.assertEqual(len(self.d.coalesce([b, a], 161)), 2)
        urgent = self.candidate({**self.small, "_attention": True})
        self.assertIn(urgent, self.d.coalesce([a, urgent], 200))
        failed = self.candidate(self.small, failures=1)
        self.assertIn(failed, self.d.coalesce([a, failed], 200))

    def test_incomplete_or_undated_result_cannot_cover_narrow_window(self):
        self.assertIsNone(project("tickets", self.broad, self.small, {"complete": False, "rows": []}))
        self.assertIsNone(
            project(
                "tickets", self.broad, self.small, {"complete": True, "rows": [{"id": "unknown", "status": "待处理"}]}
            )
        )

    def test_broader_range_does_not_mark_narrower_task_successful(self):
        from datetime import datetime
        from backend.business_reader import BEIJING

        stamp = datetime.fromtimestamp(self.small["start"] + 1, BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        self.d.replace(self.shop, [("tickets", self.broad, "订阅"), ("tickets", self.small, "常规")])
        broad_key = self.service.key("tickets", self.broad, self.shop)
        small_key = self.service.key("tickets", self.small, self.shop)
        payload = {
            "complete": True,
            "rows": [{"id": "one", "status": "待处理", "created_at": stamp}],
            "_sync": {"snapshot_started_at": time.time()},
        }
        self.d.publish(self.shop, "tickets", self.broad, broad_key, payload)
        self.assertIsNone(self.store.read_cache(self.sid, small_key))
        self.assertFalse((collection.state(self.store, self.sid, small_key) or {}).get('last_success'))

    def test_complete_evidence_can_satisfy_focus_but_ticket_quick_filter_cannot(self):
        o = options_for("evidence", status='待举证')
        focus = {**o, "_attention": True}
        p = {"complete": True, "rows": [{"id": "1", "status": "待举证"}, {"id": "2", "status": "已处理"}]}
        self.assertEqual([r["id"] for r in project("evidence", o, focus, p)["rows"]], ["1"])
        self.assertFalse(covers("tickets", self.broad, {**self.broad, "_attention": True}))

    def test_running_or_manual_consumer_not_overwritten(self):
        self.d.replace(self.shop, [("tickets", self.small, "查询")])
        key = self.service.key("tickets", self.small, self.shop)
        self.service.progress[(self.sid, key)] = {"phase": "running"}
        self.d.publish(
            self.shop,
            "tickets",
            self.broad,
            "producer",
            {"complete": True, "rows": [], "_sync": {"snapshot_started_at": time.time()}},
        )
        self.assertIsNone(self.store.read_cache(self.sid, key))

    def test_queue_obeys_node_shop_and_active_shop_limits_without_reservations(self):
        from backend.tests.collection_fixture import policy

        c = policy(max_concurrent=4, shop_concurrent=2, max_active_shops=4)

        def job(kind="evidence", **kw):
            return {"kind": kind, "scope": {}, "phase": "queued", "queued_at": 1, **kw}

        tasks = {(i, "key"): job(failures=1) for i in range(1, 7)}
        self.assertEqual(len(select_jobs(tasks, c, now=10)), 4)
        same = {(1, str(i)): job() for i in range(6)}
        self.assertEqual(len(select_jobs(same, c, now=10)), 1)
        tasks[(1, "key")]["retry_at"] = 100
        self.assertNotIn((1, "key"), select_jobs(tasks, c, now=10))
        self.assertTrue(all(queue_status(tasks, c)[g["id"]]["reserved"] == 0 for g in c["frequency_groups"]))

    def test_admission_has_no_hidden_home_reservation(self):
        from backend.admission import choose

        config = collection.DEFAULTS
        waiting = {(sid, "old"): {"kind": "orders", "scope": {}, "phase": "queued"} for sid in range(1, 11)}
        candidates = [({"id": 20}, None, "overview", {}, {}), ({"id": 21}, None, "orders", {}, {})]
        selected, deferred, caps = choose(candidates, waiting, config, time.time())
        self.assertEqual([c[2] for c in selected], ["overview", "orders"])
        self.assertEqual(deferred, [])
        self.assertEqual(set(caps), {"focus", "normal"})

    def test_queue_filter_keeps_shop_and_state_constraints(self):
        from backend.source_pages import source_page

        data = {
            "sources": [
                {
                    "shop_id": 1,
                    "kind": "tickets",
                    "queue": "normal",
                    "queues": ["focus", "normal"],
                    "state": "logged_in",
                    "data_status": "updated",
                },
                {"shop_id": 2, "kind": "tickets", "queue": "focus", "state": "logged_in", "data_status": "updated"},
            ]
        }
        self.assertEqual([r["shop_id"] for r in source_page(data, shop_id="1", cadence="focus")["rows"]], [1])


class CompletedQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_different_time_window_requires_its_own_query(self):
        from backend.request_reuse import complete, fresh
        from unittest.mock import AsyncMock
        from datetime import datetime
        from backend.business_reader import BEIJING

        session = SimpleNamespace(shop={"platform": "pdd"}, state="logged_in")
        broad = options_for("tickets", status="待处理")
        small = {**broad, "start": broad["start"] + 86400 * 170}
        row = {
            "id": "1",
            "status": "待处理",
            "created_at": datetime.fromtimestamp(small["start"] + 1, BEIJING).strftime("%Y-%m-%d %H:%M:%S"),
        }
        reader = AsyncMock(return_value={"rows": [row], "complete": True})
        await complete(session, "tickets", broad, reader)
        result = await complete(session, "tickets", small, reader)
        self.assertEqual(reader.await_count, 2)
        self.assertEqual(result["rows"], [row])
        with fresh():
            await complete(session, "tickets", small, reader)
        self.assertEqual(reader.await_count, 3)

    async def test_incomplete_query_cannot_suppress_another_read(self):
        from backend.request_reuse import complete
        from unittest.mock import AsyncMock

        session = SimpleNamespace(shop={"platform": "pdd"}, state="logged_in")
        reader = AsyncMock(return_value={"rows": [], "complete": False})
        options = options_for("evidence")
        await complete(session, "evidence", options, reader)
        await complete(session, "evidence", options, reader)
        self.assertEqual(reader.await_count, 2)
