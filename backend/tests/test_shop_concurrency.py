import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from backend.shop_lock import ShopLock, CollectionSession
from backend.collection_queue import select_jobs
from backend.tests.collection_fixture import policy

DEFAULTS = policy(max_concurrent=4, shop_concurrent=2)


class LimitsTests(unittest.TestCase):
    def test_shop_and_node_limits_both_apply(self):
        jobs = {
            (sid, str(i)): {
                "kind": ["overview", "products", "reviews", "tickets", "evidence", "packages"][i],
                "phase": "queued",
                "queued_at": i,
                "scope": {},
            }
            for sid in (1, 2)
            for i in range(6)
        }
        for limit in (1, 2, 3):
            chosen = select_jobs(jobs, {**DEFAULTS, "shop_concurrent": limit})
            self.assertEqual(len(chosen), min(4, limit * 2))
            for sid in (1, 2):
                self.assertLessEqual(sum(k[0] == sid for k in chosen), limit)
        self.assertEqual(len(select_jobs({k: v for k, v in jobs.items() if k[0] == 1}, DEFAULTS)), 2)


class LockTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_slots_keep_shop_busy_until_last_reader_finishes(self):
        from backend.business import BusinessService

        service = BusinessService(None, None)
        service.configure_queue(DEFAULTS)
        source = SimpleNamespace(lock=ShopLock())
        entered = []
        gate = asyncio.Event()

        async def run(i):
            async with service.slot((1, str(i)), CollectionSession(source), ["overview", "products", "reviews"][i]):
                entered.append(i)
                await gate.wait()

        tasks = [asyncio.create_task(run(i)) for i in range(3)]
        try:
            await asyncio.sleep(0.05)
            self.assertEqual(entered, [0, 1])
            self.assertEqual(source.lock.readers, 2)
            service.configure_queue({**DEFAULTS, "shop_concurrent": 1})
            tasks[0].cancel()
            await asyncio.gather(tasks[0], return_exceptions=True)
            await asyncio.sleep(0.05)
            self.assertEqual(entered, [0, 1])
            self.assertIn(1, service.running_shops)
            service.configure_queue({**DEFAULTS, "shop_concurrent": 3})
            await asyncio.sleep(0.05)
            self.assertEqual(entered, [0, 1, 2])
            gate.set()
            await asyncio.gather(*tasks[1:])
            self.assertFalse(service.running_shops)
            self.assertFalse(source.lock.locked())
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_parallel_reads_and_exclusive_writer_priority(self):
        lock = ShopLock()
        a = await lock.read()
        b = await lock.read()
        self.assertEqual(lock.readers, 2)
        writer = asyncio.create_task(lock.acquire())
        await asyncio.sleep(0)
        next_read = asyncio.create_task(lock.read())
        await asyncio.sleep(0)
        a.release()
        await asyncio.sleep(0)
        self.assertFalse(writer.done())
        b.release()
        await asyncio.wait_for(writer, 1)
        self.assertFalse(next_read.done())
        lock.release()
        c = await asyncio.wait_for(next_read, 1)
        c.release()
        self.assertFalse(lock.locked())

    async def test_two_failed_reads_can_recover_without_deadlock(self):
        lock = ShopLock()
        leases = [await lock.read(), await lock.read()]
        count = 0

        async def recover(lease):
            nonlocal count
            async with lease.exclusive():
                self.assertEqual(lock.readers, 0)
                count += 1
                await asyncio.sleep(0.01)
            lease.release()

        await asyncio.wait_for(asyncio.gather(*(recover(l) for l in leases)), 1)
        self.assertEqual(count, 2)
        self.assertFalse(lock.locked())

    async def test_cancelled_writer_releases_waiter_and_progress_is_local(self):
        lock = ShopLock()
        lease = await lock.read()
        task = asyncio.create_task(lock.acquire())
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(lock.waiting, 0)
        other = await asyncio.wait_for(lock.read(), 1)
        other.release()
        lease.release()
        source = SimpleNamespace(lock=lock, read_progress=None, recover_home=AsyncMock(return_value=True))
        a, b = CollectionSession(source), CollectionSession(source)
        a.read_progress = "a"
        b.read_progress = "b"
        self.assertIsNone(source.read_progress)
        self.assertEqual(a.read_progress, "a")

    async def test_cancel_during_recovery_does_not_leak_read_capacity(self):
        lock = ShopLock()
        a = await lock.read()
        b = await lock.read()

        async def recovering():
            try:
                async with a.exclusive():
                    pass
            finally:
                a.release()

        task = asyncio.create_task(recovering())
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        b.release()
        self.assertFalse(lock.locked())
        self.assertEqual(lock.waiting, 0)
