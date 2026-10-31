import asyncio
import copy
import tempfile
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from contextlib import asynccontextmanager

from backend import attention, auto_refunds, refund_priority, update_gate
from backend.direct_refunds import DirectRefunds
from backend.read_budget import current, ReadBudget
from backend.store import Store
from backend.tests.collection_fixture import policy
from backend.tests.test_auto_refunds import EXPECTED, CASE, CHECK


class DirectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add('direct', 'pw', name='direct')
        self.item = SimpleNamespace(shop=self.store.list()[0], state='logged_in', wanted_running=True, retired=False, control_until=0)
        self.service = SimpleNamespace(store=self.store, queue_config=policy(max_concurrent=1), session_getter=lambda _: self.item,
            progress={(99, 'busy'): {'phase': 'running'}}, running_shops={99}, admit=AsyncMock())
        self.worker = DirectRefunds(self.service)
        self.worker.loop = asyncio.get_running_loop()
        self.store.direct_refunds = self.worker
        update_gate.release()

    async def asyncTearDown(self):
        await self.worker.close()
        update_gate.release()
        self.temp.cleanup()

    def publish(self, case='123'):
        attention.publish(self.store, self.sid, 'aftersales', {'rows': [{**EXPECTED, 'id': case}], 'complete': True})

    async def finish(self):
        await asyncio.sleep(0)
        async with asyncio.timeout(5):
            while self.worker.tasks:
                await asyncio.gather(*list(self.worker.tasks.values()))

    async def test_publication_runs_without_admission_even_when_normal_slots_are_full(self):
        async def perform(*args, **kwargs):
            self.assertIsNone(current.get())
            self.assertTrue(self.item.direct_refund_active)
            self.assertTrue(kwargs['direct'])
            rows = refund_priority.pending(self.store, self.sid)
            refund_priority.checked(self.store, self.sid, rows, {'123'})
            return {'123'}
        with patch('backend.auto_refunds.prioritize', side_effect=perform) as action:
            # A publisher's expiring read budget must not leak into the detached operation.
            token = current.set(ReadBudget(1, {}))
            try:
                await asyncio.to_thread(self.publish)
            finally:
                current.reset(token)
            await self.finish()
            action.assert_awaited_once()
        self.service.admit.assert_not_called()
        self.assertEqual(self.service.progress, {(99, 'busy'): {'phase': 'running'}})
        self.assertFalse(self.item.direct_refund_active)

    async def test_duplicate_notifications_share_one_store_operation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def perform(*args, **kwargs):
            entered.set(); await release.wait(); return set()
        with patch('backend.auto_refunds.prioritize', side_effect=perform) as action:
            self.publish()
            await asyncio.wait_for(entered.wait(), 5)
            for _ in range(20): self.worker.notify(self.sid)
            await asyncio.sleep(0)
            self.assertEqual(len(self.worker.tasks), 1)
            self.assertEqual(action.await_count, 1)
            release.set(); await self.finish()

    async def test_paused_off_stopped_and_other_platform_do_not_execute(self):
        with patch('backend.auto_refunds.prioritize', new=AsyncMock()) as action:
            for name, value in [('state', 'stopped'), ('wanted_running', False), ('retired', True)]:
                old = getattr(self.item, name); setattr(self.item, name, value)
                self.publish(); await self.finish()
                setattr(self.item, name, old)
            self.service.queue_config['home_paused'] = True
            self.publish(); await self.finish()
            action.assert_not_awaited()
        self.item.shop['platform'] = 'jd'
        self.assertFalse(self.worker.allowed(self.item))

    async def test_updater_waits_for_direct_operation_and_does_not_launch_more(self):
        self.item.lock = asyncio.Lock()
        self.item.direct_refund_active = True
        self.assertEqual(update_gate.blockers(self.service, [self.item])['busy_windows'], 1)
        update_gate.renew()
        self.worker.offer(self.sid)
        self.assertEqual(self.worker.tasks, {})
        self.assertIn(self.sid, self.worker.dirty)

    async def test_startup_recovers_existing_cases_already_checked_by_old_queue_mode(self):
        self.worker.loop = None
        self.publish()
        from backend.collector_update import write_json
        write_json(refund_priority.receipt_path(self.store, self.sid), {'checked': {'123': EXPECTED['order_number']}})
        executed = asyncio.Event()
        async def perform(*args, **kwargs):
            executed.set(); return {'123'}
        with patch('backend.auto_refunds.prioritize', side_effect=perform):
            daemon = asyncio.create_task(self.worker.run())
            try: await asyncio.wait_for(executed.wait(), 5)
            finally:
                daemon.cancel(); await asyncio.gather(daemon, return_exceptions=True)
        self.service.admit.assert_not_called()

    async def test_persisted_rate_limit_survives_browser_or_service_restart(self):
        from backend import collection
        collection.register(self.store, self.sid, 'aftersales:limited', 'aftersales', {'_attention': True})
        collection.finish(self.store, self.sid, 'aftersales:limited', error='请求频繁', error_kind='rate_limit')
        with patch('backend.auto_refunds.prioritize', new=AsyncMock()) as action:
            self.publish(); await self.finish()
            action.assert_not_awaited()
        self.assertGreater(self.worker.retry_at[self.sid], time.monotonic() + 280)

    async def test_cancel_before_first_coroutine_step_does_not_leave_a_task_placeholder(self):
        self.worker.offer(self.sid)
        await self.worker.cancel_shop(self.sid)
        self.assertNotIn(self.sid, self.worker.tasks)

    async def test_deleted_shop_does_not_leave_an_infinite_retry_demand(self):
        def removed(_): raise KeyError(self.sid)
        self.service.session_getter = removed
        self.worker.offer(self.sid); await self.finish()
        self.assertNotIn(self.sid, self.worker.dirty)
        self.assertNotIn(self.sid, self.worker.retry_at)

    async def test_refund_endpoint_rate_limit_is_not_retried_inside_cooldown(self):
        page = SimpleNamespace(evaluate=AsyncMock(return_value={'success': False, 'errorMsg': '请求频繁'}), session=self.item)
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, '限流|冷却'):
                await auto_refunds.request(page, auto_refunds.PRECHECK, {'items': []})
        self.assertEqual(page.evaluate.await_count, 1)

    async def test_platform_cooldown_retains_direct_demand_without_opening_browser(self):
        self.publish(); await self.worker.cancel_shop(self.sid)
        from backend.operations import ENDPOINTS
        self.item._read_cooldowns = {ENDPOINTS['aftersales']: time.monotonic() + 300}
        with patch('backend.auto_refunds.prioritize', new=AsyncMock()) as action:
            self.worker.offer(self.sid); await self.finish()
            action.assert_not_awaited()
        self.assertIn(self.sid, self.worker.dirty)
        self.assertGreater(self.worker.retry_at[self.sid], time.monotonic() + 290)
        decision = auto_refunds.records(self.store, self.sid)['rows'][0]
        self.assertEqual(decision['state'], 'retry')
        self.assertIn('平台限流', decision['reason'])
        self.assertFalse(auto_refunds.record_path(self.store, self.sid, '123').exists())

    async def test_page_failure_is_a_visible_retry_receipt_not_a_financial_claim(self):
        from backend.read_errors import PageReadError
        page = SimpleNamespace(open=AsyncMock(side_effect=PageReadError('登录已失效，请重新登录')))
        @asynccontextmanager
        async def borrow(*args): yield page
        with patch('backend.read_page_pool.borrow', borrow):
            self.publish(); await self.finish()
        rows = auto_refunds.records(self.store, self.sid)['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['state'], 'retry')
        self.assertEqual(rows[0]['record_type'], 'verification')
        self.assertIn('登录已失效', rows[0]['reason'])
        self.assertGreater(rows[0]['retry_at'], time.time())
        self.assertFalse(auto_refunds.record_path(self.store, self.sid, '123').exists())

    async def test_slow_page_preparation_cannot_consume_the_refund_operation_budget(self):
        async def slow_open(_): await asyncio.sleep(.07)
        page = SimpleNamespace(open=slow_open)
        @asynccontextmanager
        async def borrow(*args): yield page
        async def process(*args, **kwargs):
            await asyncio.sleep(.04)
            kwargs['attempted'].add('123')
            kwargs['outcomes']['123'] = {'state':'skipped', 'reason':'test verified', 'checked_at':time.time(), 'retry_at':0}
            return False, []
        self.worker.loop = None; self.publish()
        with (patch('backend.read_page_pool.borrow', borrow),
              patch('backend.auto_refunds.process', side_effect=process),
              patch('backend.auto_refunds.PREPARE_SECONDS', .2)):
            checked = await auto_refunds.prioritize(self.item, self.store, 'aftersales', {'_attention':True}, seconds=.06, direct=True)
        self.assertEqual(checked, {'123'})

    async def test_sleeping_browser_failure_is_recorded_per_case(self):
        from backend.read_errors import PageReadError
        self.item.state = 'sleeping'
        with patch('backend.browser_sleep.wake', new=AsyncMock(side_effect=PageReadError('店铺休眠唤醒超时'))):
            self.publish(); await self.finish()
        decision = auto_refunds.records(self.store, self.sid)['rows'][0]
        self.assertEqual(decision['state'], 'retry')
        self.assertIn('休眠唤醒超时', decision['reason'])

    async def test_cancellation_waits_for_operation_cleanup(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def perform(*args, **kwargs):
            entered.set()
            try: await asyncio.sleep(100)
            finally: cleaned.set()
        with patch('backend.auto_refunds.prioritize', side_effect=perform):
            self.publish(); await asyncio.wait_for(entered.wait(), 5)
            await self.worker.cancel_all()
        self.assertTrue(cleaned.is_set())
        self.assertFalse(self.item.direct_refund_active)

    async def test_legacy_queued_refund_hooks_skip_when_direct_worker_owns_policy(self):
        with patch('backend.auto_refunds._prioritize', new=AsyncMock()) as old:
            self.assertEqual(await auto_refunds.prioritize(self.item, self.store, 'aftersales', {'_attention': True}), set())
            old.assert_not_awaited()

    async def test_direct_execution_keeps_native_guards_and_durable_single_submit(self):
        page = SimpleNamespace(open=AsyncMock())
        @asynccontextmanager
        async def borrow(*args):
            yield page
        with (patch('backend.read_page_pool.borrow', borrow),
              patch('backend.operations.uncached_read', new=AsyncMock(return_value={'list': [copy.deepcopy(CASE)]})),
              patch('backend.auto_refunds.request', new=AsyncMock(side_effect=[copy.deepcopy(CHECK), {'success': True}])) as request):
            self.publish(); await self.finish()
            record = auto_refunds.load(auto_refunds.record_path(self.store, self.sid, '123'))
            self.assertEqual(record['state'], 'accepted')
            self.publish(); await self.finish()
            self.assertEqual(request.await_count, 2)  # One precheck + exactly one financial submission.
        self.item._refund_operation_lock = asyncio.Lock()
        await self.item._refund_operation_lock.acquire()
        with patch('backend.auto_refunds._apply', new=AsyncMock()) as old:
            payload = {'rows': [], 'complete': True}
            self.assertEqual(await auto_refunds.apply(self.item, self.store, 'aftersales', {'_attention': True}, payload), (payload, False))
            old.assert_not_awaited()

    async def test_busy_sqlite_executor_cannot_block_financial_file_claim(self):
        page = SimpleNamespace(open=AsyncMock())
        @asynccontextmanager
        async def borrow(*args): yield page
        release = threading.Event()
        pool = ThreadPoolExecutor(1)
        pool.submit(release.wait)
        try:
            with (patch('backend.async_database._writes', pool),
                  patch('backend.read_page_pool.borrow', borrow),
                  patch('backend.operations.uncached_read', new=AsyncMock(return_value={'list': [copy.deepcopy(CASE)]})),
                  patch('backend.auto_refunds.request', new=AsyncMock(side_effect=[copy.deepcopy(CHECK), {'success': True}]))):
                self.publish(); await self.finish()
                record = auto_refunds.load(auto_refunds.record_path(self.store, self.sid, '123'))
                self.assertEqual(record['state'], 'accepted')
        finally:
            release.set(); pool.shutdown(wait=True)
