import asyncio
import unittest
from types import SimpleNamespace
from backend.tests.collection_fixture import policy
from backend.admission import choose, make_core_room
from backend.collection_plan import core
from backend.collection_queue import select_jobs


class CorePriorityTests(unittest.TestCase):
    def test_scope_is_today_coverage_not_historical_reports_or_daily_products(self):
        for kind, scope in [
            ("overview", {}),
            ("transactions", {"trade_range": "realtime"}),
            ("ad_reports", {}),
            ("invoices", {"_attention": True}),
        ]:
            self.assertTrue(core(kind, scope))
        for kind, scope in [
            ("products", {}),
            ("orders", {}),
            ("ad_reports", {"trade_range": "yesterday"}),
            ("transactions", {"_exact_dates": True}),
        ]:
            self.assertFalse(core(kind, scope))

    def test_home_does_not_have_hidden_priority_over_older_due_work(self):
        shop = {"id": 1, "platform": "pdd"}
        cfg = {
            **policy(),
            "shop_concurrent": 1,
            "platform_queues": {"pdd": {"orders": "focus", "tickets": "focus"}},
        }
        ordinary = (shop, None, "orders", {}, {"last_success": 1, "last_attempt": 1, "priority_until": 2000})
        urgent = (shop, None, "tickets", {"_attention": True}, {"last_success": 900, "last_attempt": 900})
        selected, _, _ = choose([ordinary, urgent], {}, cfg, 1000)
        self.assertEqual(selected, [ordinary, urgent])  # discovery queues both; dispatch enforces the shop cap

    def test_worker_preserves_same_module_exclusion_and_shop_limit(self):
        cfg = {**policy(), "max_concurrent": 2, "realtime_concurrent": 1, "daily_concurrent": 1}
        p = {
            (1, "active"): {"kind": "overview", "scope": {}, "phase": "running"},
            (1, "old"): {"kind": "orders", "scope": {}, "phase": "queued", "queued_at": 1, "force": True},
            (1, "core"): {"kind": "overview", "scope": {}, "phase": "queued", "queued_at": 990},
        }
        self.assertEqual(select_jobs(p, cfg, now=1000), [(1, "old")])
        cfg["shop_concurrent"] = 1
        self.assertEqual(select_jobs(p, cfg, now=1000), [])
        cfg["shop_concurrent"] = 2
        p[(1, "old")]["failures"] = 1
        cfg["retry_concurrent"] = 0
        self.assertEqual(select_jobs(p, cfg, now=1000), [(1, "old")])


class CoreRoomTests(unittest.IsolatedAsyncioTestCase):
    async def test_home_arrival_does_not_cancel_existing_demands(self):
        cfg = policy()
        service = SimpleNamespace(progress={}, jobs={})

        async def waiting(key):
            try:
                await asyncio.sleep(100)
            finally:
                service.progress.pop(key, None)
                service.jobs.pop(key, None)

        for key, phase, force in [("list", "queued", False), ("active", "running", False), ("manual", "queued", True)]:
            identity = (1, key)
            service.progress[identity] = {"kind": "orders", "scope": {}, "phase": phase, "force": force}
            service.jobs[identity] = asyncio.create_task(waiting(identity))
        await asyncio.sleep(0)
        try:
            await make_core_room(service, [({"id": 1}, None, "overview", {}, {})], cfg)
            self.assertIn((1, "list"), service.progress)
            self.assertEqual(set(service.progress), {(1, "list"), (1, "active"), (1, "manual")})
            self.assertTrue(all(not t.cancelling() for t in service.jobs.values()))
        finally:
            tasks = list(service.jobs.values())
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
