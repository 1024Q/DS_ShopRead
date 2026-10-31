import copy, unittest
from types import SimpleNamespace
from backend import collection
from backend.collection_queue import select_jobs, waiting_reason
from backend.demand_refresh import refresh
from backend.collection_demands import Demands
from backend.admission import choose


class DemandRefreshTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            **collection.DEFAULTS,
            "queue_version": 3,
            "max_concurrent": 12,
            "realtime_concurrent": 4,
            "daily_concurrent": 4,
            "scheduled_concurrent": 4,
            "shop_concurrent": 3,
            "borrow_idle": True,
            "platform_modes": {"pdd": {"orders": "realtime", "products": "custom", "reviews": "daily"}},
        }

    def meta(self, kind, when=900, **extra):
        return {"kind": kind, "phase": "queued", "queued_at": when, "scope": {}, "platform": "pdd", **extra}

    def test_three_distinct_modules_and_same_module_serial_across_queues(self):
        p = {
            (1, "orders:a"): self.meta("orders", phase="running", queue="normal"),
            (1, "orders:b"): self.meta("orders"),
            (1, "products"): self.meta("products"),
            (1, "reviews"): self.meta("reviews"),
            (1, "tickets"): self.meta("tickets"),
        }
        before = copy.deepcopy(p)
        selected = select_jobs(p, self.config, now=1000)
        self.assertEqual(len(selected), 2)
        self.assertNotIn((1, "orders:b"), selected)
        self.assertEqual(p, before)
        self.assertIn("同模块", waiting_reason((1, "orders:b"), p[(1, "orders:b")], p, self.config))
        self.assertEqual(len(select_jobs(p, {**self.config, "shop_concurrent": 2}, now=1000)), 1)

    def test_overdue_beats_manual_and_old_core_priority(self):
        p = {
            (1, "orders"): self.meta("orders", 100, force=True),
            (2, "orders"): self.meta("orders", 900, force=True),
            (3, "overview"): self.meta("overview", 999),
        }
        selected = select_jobs(p, {**self.config, "max_concurrent": 1}, now=1000)
        self.assertEqual(selected, [(1, "orders")])
        p.pop((3, "overview"))
        self.assertEqual(select_jobs(p, {**self.config, "max_concurrent": 1}, now=1000), [(1, "orders")])

    def test_range_union_and_checkpoint_running_untouched(self):
        scope = {"start": 10, "end": 20, "date_from": "2026-10-12", "date_to": "2026-10-13", "status": "待发货"}
        newer = {**scope, "start": 15, "end": 30, "date_from": "2026-10-13", "date_to": "2026-10-14"}
        m = self.meta("orders", 100, scope=scope)
        refresh(m, newer, 1000, self.config)
        self.assertEqual((m["scope"]["start"], m["scope"]["end"]), (10, 30))
        self.assertEqual(m["queued_at"], 100)
        self.assertEqual(m["merged_periods"], 15)
        for flag in ({"continuation": True}, {"failures": 1}, {"phase": "running"}):
            m = self.meta("orders", 100, scope=scope, **flag)
            refresh(m, newer, 1000, self.config)
            self.assertEqual(m["scope"], scope)

    def test_duplicate_producers_merge_without_merging_different_status(self):
        service = SimpleNamespace(
            queue_config=self.config, progress={}, key=lambda kind, scope, shop: kind + scope.get("status", "")
        )
        demands = Demands(service)
        shop = {"id": 1, "platform": "pdd"}
        cs = [
            (shop, None, "orders", {"start": 10, "end": 20, "status": "待发货"}, {"waiting_since": 100}),
            (shop, None, "orders", {"start": 15, "end": 30, "status": "待发货"}, {"waiting_since": 200}),
            (shop, None, "orders", {"start": 15, "end": 30, "status": "待收货"}, {"waiting_since": 200}),
        ]
        out = demands.coalesce(cs, 1000)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0][3]["start"], 10)
        self.assertEqual(out[0][3]["end"], 30)
        self.assertEqual(out[0][4]["waiting_since"], 100)

    def test_admission_promotes_overdue_without_changing_scope(self):
        cs = [
            ({"id": sid, "platform": "pdd"}, None, "orders", {"status": "待发货"}, {"waiting_since": since})
            for sid, since in [(1, 900), (2, 100)]
        ]
        before = copy.deepcopy(cs)
        selected, _, _ = choose(cs, {}, self.config, 1000)
        self.assertEqual(selected[0][0]["id"], 2)
        self.assertEqual(cs, before)

    def test_pending_order_scopes_cannot_fill_all_shop_queue_slots(self):
        shop = {"id": 1, "platform": "pdd"}
        cs = [
            (shop, None, "orders", {"status": status}, {"waiting_since": 100})
            for status in ("待付款", "待发货", "待收货")
        ]
        cs.append((shop, None, "products", {}, {"waiting_since": 900}))
        selected, deferred, _ = choose(cs, {}, self.config, 1000)
        self.assertEqual([c[2] for c in selected].count("orders"), 1)
        self.assertIn("products", [c[2] for c in selected])
        self.assertEqual(len(deferred), 2)

    def test_deadline_and_merge_time_follow_queue_without_resetting_generation(self):
        from backend.demand_refresh import timing

        for kind, seconds in [("orders", 60), ("products", 300)]:
            m = self.meta(kind, 100)
            self.assertEqual(timing(kind, {}, m, self.config, "pdd", 100)["queue_deadline"], 100 + seconds)
            refresh(m, {}, 100 + seconds, self.config)
            self.assertEqual(m["last_merged_at"], 100 + seconds)
            refresh(m, {}, 101 + seconds, self.config)
            self.assertEqual(m["last_merged_at"], 100 + seconds)
            self.assertEqual(m["queued_at"], 100)

    def test_duplicate_overdue_retry_cannot_skip_cooldown(self):
        m = self.meta("orders", 100, failures=1, retry_at=1100)
        refresh(m, {}, 1000, self.config)
        p = {(1, "orders"): m, (2, "orders"): self.meta("orders", 900)}
        self.assertEqual(select_jobs(p, {**self.config, "max_concurrent": 1}, now=1000), [(2, "orders")])
        self.assertEqual(m["retry_at"], 1100)

    def test_daily_deadline_is_next_scheduled_time_and_gui_exposes_generation(self):
        from datetime import datetime
        from backend.business_reader import BEIJING
        from backend.demand_refresh import timing
        from backend.collector_task_list import listing

        since = datetime(2026, 10, 14, 6, tzinfo=BEIJING).timestamp()
        m = self.meta("reviews", since)
        t = timing("reviews", {}, m, {**self.config, "daily_time": "05:00"}, "pdd", since)
        self.assertEqual(datetime.fromtimestamp(t["queue_deadline"], BEIJING).isoformat(), "2026-10-15T05:00:00+08:00")
        service = SimpleNamespace(
            queue_config=self.config,
            store=SimpleNamespace(list=lambda: [{"id": 1, "name": "test", "platform": "pdd"}]),
            progress={(1, "reviews"): m},
        )
        row = listing(service, "queued", 1, {}, {}, [])["rows"][0]
        self.assertEqual(row["generated_at"], since)
        self.assertEqual(row["generated"], "10-14 06:00:00")


class ModuleHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_module_waits_then_starts_without_interrupting_running_read(self):
        import asyncio
        from backend.business import BusinessService

        service = BusinessService(None, None)
        service.queue_config.update(max_concurrent=3, shop_concurrent=3, borrow_idle=True)
        started = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()

        async def run(index):
            item = SimpleNamespace(lock=asyncio.Lock(), shop={"id": 1})
            async with service.slot((1, "orders:" + str(index)), item, "orders"):
                started[index].set()
                if index == 0:
                    await release.wait()

        first = asyncio.create_task(run(0))
        await asyncio.wait_for(started[0].wait(), 1)
        second = asyncio.create_task(run(1))
        try:
            await asyncio.sleep(0.03)
            self.assertFalse(started[1].is_set())
            self.assertFalse(first.done())
            release.set()
            await asyncio.wait_for(started[1].wait(), 1)
        finally:
            release.set()
            await asyncio.gather(first, second)
