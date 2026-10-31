import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from backend import collection
from backend.business import BusinessService
from backend.collection_queue import select_jobs
from backend.frequency_policy import migrate
from backend.shop_lock import ShopLock


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = BusinessService(None, None)
        self.service.queue_config = migrate(copy.deepcopy(collection.DEFAULTS))
        self.service.queue_config["max_concurrent"] = 1
        self.jobs = []

    async def asyncTearDown(self):
        for job in self.jobs:
            job.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        await self.service.close()

    def start(self, sid, kind, entered, release, lock=None, key=None):
        item = SimpleNamespace(lock=lock or ShopLock(), shop={"id": sid})

        async def run():
            async with self.service.slot((sid, key or kind), item, kind):
                entered.set()
                await release.wait()

        job = asyncio.create_task(run())
        self.jobs.append(job)
        return job

    async def test_hundreds_of_waiters_share_one_selection_and_refill_immediately(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        first = self.start(1, "overview", entered, release)
        await asyncio.wait_for(entered.wait(), 0.5)
        next_entered = asyncio.Event()
        hold = asyncio.Event()
        with patch.object(self.service.due_plans.core, "choose", wraps=self.service.due_plans.core.choose) as choose:
            for i in range(300):
                self.start(i + 2, "orders", next_entered, hold)
            await asyncio.sleep(0.08)
            self.assertFalse(next_entered.is_set())
            self.assertLessEqual(choose.call_count, 2, "each waiter must not sort the whole queue")
            release.set()
            await asyncio.wait_for(next_entered.wait(), 0.5)
            await first
            self.assertEqual(sum(m["phase"] == "running" for m in self.service.progress.values()), 1)
            self.assertLessEqual(choose.call_count, 4)

    async def test_grants_preserve_node_shop_and_parent_module_limits(self):
        self.service.queue_config.update(max_concurrent=3, shop_concurrent=2)
        hold = asyncio.Event()
        entered = [asyncio.Event() for _ in range(4)]
        self.start(1, "orders", entered[0], hold, key="today")
        self.start(1, "orders", entered[1], hold, key="yesterday")
        self.start(1, "products", entered[2], hold)
        self.start(2, "overview", entered[3], hold)
        await asyncio.wait_for(asyncio.gather(*(entered[i].wait() for i in (0, 2, 3))), 0.5)
        self.assertFalse(entered[1].is_set())
        self.assertEqual(sum(m["phase"] == "running" for m in self.service.progress.values()), 3)
        self.assertFalse(self.service.slot_dispatcher.grants)
        self.jobs[0].cancel()
        await asyncio.gather(self.jobs[0], return_exceptions=True)
        await asyncio.wait_for(entered[1].wait(), 0.5)
        self.assertEqual(sum(m["phase"] == "running" for m in self.service.progress.values()), 3)

    async def test_configuration_increase_wakes_waiter_without_timer(self):
        hold = asyncio.Event()
        first = asyncio.Event()
        second = asyncio.Event()
        self.start(1, "overview", first, hold)
        self.start(2, "overview", second, hold)
        await asyncio.wait_for(first.wait(), 0.5)
        self.assertFalse(second.is_set())
        self.service.configure_queue({**self.service.queue_config, "max_concurrent": 2})
        await asyncio.wait_for(second.wait(), 0.5)

    async def test_configuration_decrease_applies_to_new_reservations_without_interrupting_readers(self):
        self.service.queue_config['max_concurrent'] = 2
        hold = asyncio.Event()
        entered = [asyncio.Event() for _ in range(3)]
        for i in range(2):self.start(i+1, 'overview', entered[i], hold)
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in entered[:2])), 1)
        self.service.configure_queue({**self.service.queue_config, 'max_concurrent':1})
        self.start(3, 'overview', entered[2], hold)
        await asyncio.sleep(.03)
        self.assertFalse(entered[2].is_set())
        self.assertTrue(all(not t.cancelling() for t in self.jobs[:2]))

    async def test_cancelled_waiter_is_removed_and_browser_recovery_is_polled_once(self):
        lock = ShopLock()
        await lock.acquire()
        first = asyncio.Event()
        second = asyncio.Event()
        hold = asyncio.Event()
        cancelled = self.start(1, "orders", first, hold, lock)
        self.start(1, "overview", second, hold, lock)
        await asyncio.sleep(0.02)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        self.assertNotIn((1, "orders"), self.service.slot_dispatcher.direct)
        self.assertTrue(lock.writer)
        lock.release()
        await asyncio.wait_for(second.wait(), 1.3)
        self.assertFalse(first.is_set())


class FullQueueTests(unittest.TestCase):
    def test_full_capacity_does_not_sort_waiting_demands(self):
        config = migrate(copy.deepcopy(collection.DEFAULTS))
        config["max_concurrent"] = 1
        progress = {
            (1, "active"): {"kind": "overview", "phase": "running"},
            (2, "waiting"): {"kind": "orders", "phase": "queued"},
        }
        with patch("backend.demand_refresh.order", side_effect=AssertionError("unneeded full-queue sort")):
            self.assertEqual(select_jobs(progress, config), [])
