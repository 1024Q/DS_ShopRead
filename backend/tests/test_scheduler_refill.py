import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from backend.scheduler import Scheduler
from backend.collector_task_list import listing, deferred_categories


class RefillTests(unittest.IsolatedAsyncioTestCase):
    async def test_feeder_replenishes_buffer_without_another_shop_scan(self):
        from backend.business import BusinessService

        service = BusinessService(None, lambda _: None)
        service.queue_config.update(max_concurrent=2, shop_concurrent=1)
        scheduler = Scheduler(service)
        for sid in range(1, 9):
            shop = {"id": sid, "platform": "pdd", "profile_version": 0}
            item = SimpleNamespace(retired=False, state="logged_in")
            scheduler.pending.append((shop, item, "products", {}, {}))

        def prepare(candidates, config):
            return [(s, i, k, (o, service.key(k, o, s), None, config, {}), t) for s, i, k, o, t in candidates]

        def admit(shop, item, kind, plan, *args, **kwargs):
            key = (shop["id"], plan[1])
            service.jobs[key] = object()
            service.progress[key] = {"kind": kind, "phase": "queued", "scope": plan[0], "queued_at": 1}

        service.prepare_scheduled = prepare
        service.admit = admit
        await scheduler.refill()
        self.assertEqual(len(service.progress), 8)
        self.assertEqual(len(scheduler.pending), 0)
        for key in list(service.progress)[:2]:
            service.progress.pop(key)
            service.jobs.pop(key)
        await scheduler.refill()
        self.assertEqual(len(service.progress), 6)
        self.assertEqual(scheduler.pending, [])

    async def test_queued_job_takes_released_slot_without_scheduler_scan(self):
        from backend.business import BusinessService

        service = BusinessService(None, lambda _: None)
        service.queue_config.update(max_concurrent=2, realtime_concurrent=1, daily_concurrent=1, borrow_idle=True)
        entered = [asyncio.Event() for _ in range(3)]
        release = [asyncio.Event() for _ in range(3)]

        async def work(index):
            item = SimpleNamespace(lock=asyncio.Lock(), shop={"id": index + 1})
            async with service.slot((index + 1, "overview"), item, "overview"):
                entered[index].set()
                await release[index].wait()

        jobs = [asyncio.create_task(work(i)) for i in range(3)]
        try:
            await asyncio.wait_for(asyncio.gather(entered[0].wait(), entered[1].wait()), 1)
            self.assertFalse(entered[2].is_set())
            release[0].set()
            await asyncio.wait_for(entered[2].wait(), 0.5)
            self.assertEqual(sum(m["phase"] == "running" for m in service.progress.values()), 2)
        finally:
            for event in release:
                event.set()
            await asyncio.gather(*jobs)

    def test_pressured_shop_does_not_reserve_an_idle_slot(self):
        from backend.collection_queue import select_jobs
        from backend import collection

        config = {**collection.DEFAULTS, "max_concurrent": 1, "realtime_concurrent": 1, "daily_concurrent": 1}
        meta = {"kind": "overview", "phase": "queued", "queued_at": 1, "scope": {}}
        progress = {(1, "overview"): {**meta, "database_pressure": True}, (2, "overview"): dict(meta)}
        self.assertEqual(select_jobs(progress, config), [(2, "overview")])

    async def test_refill_runs_while_shop_scan_is_blocked(self):
        service = SimpleNamespace(schedule_wake=asyncio.Event())
        scheduler = Scheduler(service)
        twice = asyncio.Event()
        scanning = asyncio.Event()
        calls = []

        async def tick():
            scanning.set()
            await asyncio.Event().wait()

        async def refill():
            await scanning.wait()
            calls.append(1)
            if len(calls) == 1:
                service.schedule_wake.set()
            else:
                twice.set()

        scheduler.tick = tick
        scheduler.refill = refill
        task = asyncio.create_task(scheduler.run())
        try:
            await asyncio.wait_for(twice.wait(), 3)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(calls), 2)

    async def test_shop_scan_is_bounded_and_failure_isolated(self):
        service = SimpleNamespace(store=SimpleNamespace(log_event=Mock()))
        scheduler = Scheduler(service)
        active = 0
        peak = 0
        finished = []

        async def scan(shop, config):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.02)
                if shop["id"] == 2:
                    raise ValueError("fixture")
                finished.append(shop["id"])
            finally:
                active -= 1

        scheduler.tick_shop = scan
        await scheduler.inspect_shops([{"id": n} for n in range(10)], {})
        self.assertEqual(peak, 4)
        self.assertEqual(len(finished), 9)
        service.store.log_event.assert_called_once()

    async def test_shutdown_cancels_maintenance(self):
        scheduler = Scheduler(SimpleNamespace(schedule_wake=asyncio.Event()))
        entered = asyncio.Event()

        async def tick():
            entered.set()
            await asyncio.Event().wait()

        scheduler.tick = tick
        maintenance = asyncio.create_task(asyncio.sleep(60))
        scheduler.maintenance[1] = maintenance
        task = asyncio.create_task(scheduler.run())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(maintenance.cancelled())


class DeferredTests(unittest.TestCase):
    def test_active_work_is_not_counted_twice_and_reasons_are_visible(self):
        service = SimpleNamespace(
            store=SimpleNamespace(list=lambda: []),
            progress={(1, "active"): {}},
            deferred={
                (1, "active"): {"kind": "orders", "stage": "等待采集名额"},
                (1, "due"): {"kind": "orders", "stage": "等待采集名额", "scope": {"_order_lane": "open"}},
                (1, "manual"): {"kind": "orders", "stage": "需要人工处理"},
                (1, "history"): {"kind": "orders", "stage": "历史补采等待空闲"},
            },
        )
        result = listing(service, "deferred", 1, {}, {}, [])
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["categories"], deferred_categories(service))
        self.assertEqual(result["categories"]["到期待执行"], 1)
        self.assertTrue(any("未完结回查" in r["scope"] for r in result["rows"]))
