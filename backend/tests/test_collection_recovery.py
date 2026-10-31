import asyncio
import tempfile
import time
import unittest
from types import SimpleNamespace
from backend import collection
from backend.tests.collection_fixture import policy
from backend.admission import choose
from backend.collection_queue import select_jobs
from backend.collection_recovery import release_orphans, watchdog
from backend.store import Store


class FairnessTests(unittest.TestCase):
    def test_yielded_unfinished_orders_do_not_jump_before_stale_home(self):
        config = {
            **policy(),
            "shop_concurrent": 1,
            "platform_queues": {"pdd": {"orders": "focus", "overview": "focus"}},
        }
        shop = {"id": 1, "platform": "pdd"}
        orders = (
            shop,
            None,
            "orders",
            {"_order_lane": "open"},
            {"last_success": 0, "last_attempt": 990, "continuation": 1},
        )
        home = (shop, None, "overview", {}, {"last_success": 200, "last_attempt": 200})
        selected, _, _ = choose([orders, home], {}, config, 1000)
        self.assertEqual([c[2] for c in selected], ["overview", "orders"])
        home[-1].update(last_success=1001, last_attempt=1001)
        # Once the home scope is fresh it is no longer a scheduler candidate.
        selected, _, _ = choose([orders], {}, config, 1100)
        self.assertEqual([c[2] for c in selected], ["orders"])

    def test_discovery_does_not_reserve_hidden_core_queue_space(self):
        config = {**policy(), "platform_queues": {"pdd": {"orders": "focus", "overview": "focus"}}}
        waiting = {(i, "orders"): {"phase": "queued", "kind": "orders", "scope": {}} for i in range(6)}
        candidates = [({"id": 20}, None, "overview", {}, {}), ({"id": 21}, None, "orders", {}, {})]
        selected, deferred, _ = choose(candidates, waiting, config, 1000)
        self.assertEqual([c[2] for c in selected], ["overview", "orders"])
        self.assertEqual(deferred, [])

    def test_shop_second_slot_never_runs_same_module_concurrently(self):
        cfg = {
            **policy(),
            "max_concurrent": 2,
            "realtime_concurrent": 1,
            "daily_concurrent": 1,
            "platform_queues": {"pdd": {"orders": "focus", "tickets": "focus"}},
        }
        progress = {
            (1, "first"): {"phase": "running", "kind": "orders", "scope": {}},
            (1, "second"): {"phase": "queued", "kind": "orders", "scope": {}, "queued_at": 100},
            (1, "focus"): {"phase": "queued", "kind": "tickets", "scope": {"_attention": True}, "queued_at": 200},
        }
        self.assertEqual(select_jobs(progress, cfg, now=1000), [(1, "focus")])
        del progress[(1, "focus")]
        self.assertEqual(select_jobs(progress, cfg, now=1000), [])


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("test", "pw")

    def tearDown(self):
        self.temp.cleanup()

    async def test_orphan_lease_released_without_resetting_backoff_or_active_task(self):
        now = time.time()
        for key in ("orphan", "active", "recent"):
            collection.register(self.store, self.sid, key, "overview", {})
            with self.store.write_connection() as db:
                db.execute(
                    "UPDATE collection_tasks SET lease_until=?,last_attempt=?,retry_at=?,blocked=1 WHERE cache_key=?",
                    (now + 500, now - (5 if key == "recent" else 100), now + 300, key),
                )
        self.assertEqual(release_orphans(self.store, self.sid, {"active"}, now), 1)
        orphan = collection.state(self.store, self.sid, "orphan")
        self.assertEqual(orphan["lease_until"], 0)
        self.assertTrue(orphan["blocked"])
        self.assertGreater(orphan["retry_at"], now)
        self.assertGreater(collection.state(self.store, self.sid, "active")["lease_until"], now)
        self.assertGreater(collection.state(self.store, self.sid, "recent")["lease_until"], now)

    async def test_watchdog_cancels_only_abnormally_long_running_task(self):
        tasks = [asyncio.create_task(asyncio.sleep(100)) for _ in range(3)]
        service = SimpleNamespace(
            store=self.store,
            wake=asyncio.Event(),
            jobs={(self.sid, str(i)): t for i, t in enumerate(tasks)},
            progress={
                (self.sid, "0"): {"kind": "orders", "phase": "running", "started_at": 1},
                (self.sid, "1"): {"kind": "orders", "phase": "running", "started_at": 500},
                (self.sid, "2"): {"kind": "orders", "phase": "queued", "queued_at": 1},
            },
        )
        try:
            await watchdog(service, now=1000)
            self.assertTrue(tasks[0].cancelling())
            self.assertFalse(tasks[1].cancelling())
            self.assertFalse(tasks[2].cancelling())
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_claim_records_each_batch_turn_without_changing_success(self):
        collection.register(self.store, self.sid, "key", "overview", {})
        self.assertTrue(collection.claim(self.store, self.sid, "key", now=1000))
        state = collection.state(self.store, self.sid, "key")
        self.assertEqual(state["last_attempt"], 1000)
        self.assertEqual(state["last_success"], 0)
        collection.yield_batch(self.store, self.sid, "key")
        self.assertTrue(collection.claim(self.store, self.sid, "key", now=1100))
        self.assertEqual(collection.state(self.store, self.sid, "key")["last_attempt"], 1100)
