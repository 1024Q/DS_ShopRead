import asyncio
import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from backend import collection
from backend.business import BusinessService, TaskMeta
from backend.frequency_policy import migrate
from backend.log_summary import compact


class DuePlanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = BusinessService(None, None)
        self.service.queue_config = {**migrate(copy.deepcopy(collection.DEFAULTS)), 'max_concurrent': 2,
                                     'shop_concurrent': 3, 'max_active_shops': 2, 'home_paused': False}
        self.gate = asyncio.Event()
        self.started = []
        async def sync(item, kind, scope, key):
            job_key = (item.shop['id'], key)
            try:
                async with self.service.slot(job_key, item, kind):
                    self.started.append(job_key)
                    await self.gate.wait()
            finally:
                self.service.jobs.pop(job_key, None)
                self.service.due_plans.dispatch()
        self.service.sync = sync

    async def asyncTearDown(self):
        await self.service.close()

    def add(self, sid, key, kind='overview'):
        item = SimpleNamespace(shop={'id': sid, 'platform': 'pdd'}, state='logged_in', retired=False,
                               lock=asyncio.Lock())
        meta = TaskMeta(kind=kind, scope={'trade_range': 'realtime'}, platform='pdd', queued_at=time.time(),
                        last_success=0, phase='waiting', force=False)
        self.service.due_plans.add((sid, key), item, meta)

    async def test_many_due_plans_create_only_granted_workers_and_complete_immediately_refills(self):
        for sid in range(1, 21):self.add(sid, 'one')
        self.service.due_plans.dispatch()
        self.assertEqual(len(self.service.jobs), 2)
        self.assertEqual(len(self.service.due_plans.entries), 20)
        await asyncio.sleep(.01)
        self.assertEqual(len(self.started), 2)
        self.gate.set()
        for _ in range(100):
            await asyncio.sleep(.01)
            if not self.service.jobs and not any(self.service.due_plans.pending(k) for k in self.service.due_plans.entries):break
        self.assertEqual(len(self.started), 20)
        self.assertFalse(self.service.jobs)
        self.assertFalse(any(self.service.due_plans.pending(k) for k in self.service.due_plans.entries))

    async def test_repeated_due_intent_retains_first_time_and_does_not_create_a_worker(self):
        self.add(1, 'same'); old = self.service.due_plans.entries[1, 'same'][1]['queued_at']
        for _ in range(20):self.add(1, 'same')
        self.assertEqual(len(self.service.due_plans.entries), 1)
        self.assertEqual(self.service.due_plans.entries[1, 'same'][1]['queued_at'], old)
        self.assertFalse(self.service.jobs)

    async def test_same_business_mutex_is_kept_even_when_capacity_remains(self):
        self.add(1, 'today');self.add(1, 'yesterday');self.add(1, 'trade', 'transactions')
        self.service.due_plans.dispatch()
        self.assertEqual(len(self.service.jobs), 2)
        self.assertEqual(sum(k[1] in ('today', 'yesterday') for k in self.service.jobs), 1)
        self.assertEqual(len(self.service.due_plans.entries), 3)

    async def test_disabled_and_stopped_plans_do_not_open_browser(self):
        self.add(1, 'one');self.add(2, 'two')
        self.service.due_plans.entries[1, 'one'][0].state = 'stopped'
        self.service.queue_config['home_paused'] = True
        self.service.due_plans.dispatch()
        self.assertFalse(self.service.jobs)
        self.assertTrue(all(m[1]["wait_code"] == "paused" for m in self.service.due_plans.entries.values()))

    async def test_lower_limit_keeps_running_work_and_does_not_start_more(self):
        for sid in range(1, 5):self.add(sid, 'one')
        self.service.due_plans.dispatch();await asyncio.sleep(.01)
        self.service.queue_config['max_concurrent'] = 1
        self.service.due_plans.dispatch()
        self.assertEqual(len(self.service.jobs), 2)
        self.assertTrue(all(not t.cancelling() for t in self.service.jobs.values()))

    async def test_browser_exclusive_wait_is_plan_only(self):
        self.add(1, 'one')
        item, _ = self.service.due_plans.entries[1, 'one']
        await item.lock.acquire()
        self.service.due_plans.dispatch()
        self.assertFalse(self.service.jobs)
        self.assertIn('浏览器', self.service.deferred[1, 'one']['stage'])
        item.lock.release();self.service.due_plans.dispatch()
        self.assertEqual(len(self.service.jobs), 1)


class LogSummaryTests(unittest.TestCase):
    def test_display_combines_only_identical_errors_and_preserves_shop_and_task_identity(self):
        rows = [('error', '业务事件', '2026-10-31 12:00:00 [orders] 店铺A 超时'),
                ('info', '业务事件', '任务成功'),
                ('error', '业务事件', '2026-10-31 12:01:00 [orders] 店铺A 超时'),
                ('error', '业务事件', '2026-10-31 12:01:00 [orders] 店铺B 超时')]
        result = compact(rows)
        self.assertEqual(len(result), 3)
        self.assertIn('重复 2 次', result[0][2])
        self.assertIn('首次 2026-10-31 12:00:00', result[0][2])
        self.assertIn('最近 2026-10-31 12:01:00', result[0][2])
        self.assertEqual(len(rows), 4)
