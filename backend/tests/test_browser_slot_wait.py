import asyncio
import copy
import unittest
from types import SimpleNamespace
from backend.business import BusinessService
from backend import collection
from backend.frequency_policy import migrate
from backend.shop_lock import ShopLock
from backend.collection_queue import waiting_reason


class BrowserSlotTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_wait_does_not_consume_capacity_or_execution_timeout(self):
        service = BusinessService(None, None)
        service.queue_config = migrate(copy.deepcopy(collection.DEFAULTS))
        service.queue_config["max_concurrent"] = 1
        for g in service.queue_config["frequency_groups"]:
            g["timeout"] = 0.05
        lock = ShopLock()
        await lock.acquire()
        item = SimpleNamespace(lock=lock, shop={"id": 1})
        entered = asyncio.Event()

        async def blocked():
            async with service.slot((1, "overview"), item, "overview") as meta:
                self.assertEqual(meta["phase"], "running")
                entered.set()

        job = asyncio.create_task(blocked())
        try:
            await asyncio.sleep(0.08)
            self.assertFalse(entered.is_set())
            self.assertFalse(job.done())
            meta = service.progress[(1, "overview")]
            self.assertEqual(meta["phase"], "queued")
            self.assertNotIn("started_at", meta)
            self.assertEqual(
                "等待店铺浏览器独占操作（登录或维护中）",
                waiting_reason((1, "overview"), meta, service.progress, service.queue_config),
            )
            other = SimpleNamespace(lock=ShopLock(), shop={"id": 2})
            async with service.slot((2, "products"), other, "products"):
                pass
            self.assertTrue(lock.writer)
            lock.release()
            service.wake.set()
            await asyncio.wait_for(job, 1)
            self.assertTrue(entered.is_set())
            self.assertFalse(lock.locked())
        finally:
            if lock.writer:
                lock.release()
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def test_cancelled_wait_does_not_release_login_lock(self):
        service = BusinessService(None, None)
        lock = ShopLock()
        await lock.acquire()

        async def work():
            async with service.slot((1, "overview"), SimpleNamespace(lock=lock), "overview"):
                self.fail("browser is held by login")

        job = asyncio.create_task(work())
        await asyncio.sleep(0)
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        self.assertTrue(lock.writer)
        self.assertFalse(service.progress)
        lock.release()
