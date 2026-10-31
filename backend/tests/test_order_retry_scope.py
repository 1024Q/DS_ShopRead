import asyncio, tempfile, time, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from backend import orders, collection, collection_plan
from backend.collection_errors import classify
from backend.business import options_for
from backend.store import Store
from backend.collector_task_list import listing


class ScopeTests(unittest.TestCase):
    def test_stale_default_orders_retire_but_explicit_history_remains(self):
        old = {**options_for("orders"), "date_to": "2020-01-01"}
        self.assertTrue(collection_plan.obsolete_realtime("orders", old))
        self.assertFalse(collection_plan.obsolete_realtime("orders", {**old, "explicit_dates": True}))
        self.assertEqual(classify(ValueError("下单时间超出查询范围，请刷新页面后重试")), ("range_limit", True))

    def test_rate_limit_applies_across_scopes_even_manual_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            sid = store.add("test", "pw")
            for key in ("orders:a", "orders:b"):
                collection.register(store, sid, key, "orders", options_for("orders"))
            collection.finish(store, sid, "orders:a", error="操作太过频繁")
            state = collection.state(store, sid, "orders:a")
            self.assertGreater(state["retry_at"], time.time() + 290)
            with store.connect() as db:
                db.execute("UPDATE collection_tasks SET error_kind='rate_limit' WHERE cache_key='orders:a'")
            self.assertFalse(collection.claim(store, sid, "orders:b", force=True))

    def test_task_details_group_scopes_but_preserve_child_steps(self):
        from backend.business import BusinessService
        from backend.tests.collection_fixture import policy

        service = BusinessService(None, lambda _: None)
        service.store = SimpleNamespace(list=lambda: [{"id": 1, "name": "测试", "platform": "pdd"}])
        service.configure_queue(policy())
        service.progress = {
            (1, "a"): {"kind": "orders", "scope": {"status": "待付款"}, "phase": "running"},
            (1, "b"): {"kind": "orders", "scope": {"status": "待发货"}, "phase": "queued"},
            (1, "c"): {"kind": "orders", "scope": {"status": "待收货"}, "phase": "queued"},
        }
        result = listing(service, "running", 1, {}, {}, [])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["rows"][0]["step_count"], 3)
        self.assertEqual(result["rows"][0]["running_steps"], 1)
        self.assertEqual(result["rows"][0]["queued_steps"], 2)


class RateTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_shop_cooldown_prevents_second_page_call(self):
        session = SimpleNamespace()
        a = orders.OrdersPage(session)
        b = orders.OrdersPage(session)
        a._search = AsyncMock(return_value={"success": False, "error_msg": "操作太过频繁"})
        b._search = AsyncMock()
        await a.search({})
        with self.assertRaisesRegex(ValueError, "冷却"):
            await b.search({})
        b._search.assert_not_awaited()
