import asyncio
import copy
import json
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend import attention, auto_refunds, collection, refund_priority
from backend.admission import choose
from backend.business import BusinessService
from backend.collection_queue import select_jobs
from backend.read_budget import ReadBudget
from backend.shop_rotation import ShopRotation
from backend.store import Store
from backend.tests.collection_fixture import policy
from backend.tests.collection_fixture import assign
from backend.scheduler import Scheduler
from backend.task_lifecycle import before_run
from backend.tests.test_collection_queue import job
from backend.tests.test_auto_refunds import CASE, CHECK, EXPECTED


class AdmissionTests(unittest.TestCase):
    def urgent(self, **extra):
        return job('aftersales', scope={'_attention': True}, urgent_refund=True, **extra)

    def test_waiting_platform_task_moves_ahead_without_extra_slots(self):
        tasks = {(1, 'old'): job('products', at=1), (2, 'refund'): self.urgent(at=999)}
        config = policy(max_concurrent=1, max_active_shops=2)
        self.assertEqual(select_jobs(tasks, config, now=1000), [(2, 'refund')])
        tasks[(1, 'running')] = job('orders', phase='running')
        self.assertEqual(select_jobs(tasks, config, now=1000), [])
        self.assertEqual(tasks[(1, 'running')]['phase'], 'running')

    def test_cooldown_same_module_and_off_platform_still_protect_execution(self):
        tasks = {(1, 'old'): job('products'), (2, 'refund'): self.urgent(retry_at=2000)}
        config = policy(max_concurrent=1)
        self.assertEqual(select_jobs(tasks, config, now=1000), [(1, 'old')])
        tasks[(2, 'refund')].update(retry_at=0, platform='taobao')
        self.assertFalse(refund_priority.expedited(tasks[(2, 'refund')]))
        tasks = {(2, 'active'): job('aftersales', phase='running'), (2, 'refund'): self.urgent(), (1, 'other'): job('reviews')}
        self.assertEqual(select_jobs(tasks, policy(max_concurrent=2), now=1000), [(1, 'other')])

    def test_representative_does_not_hide_urgent_scope_behind_ordinary_scope(self):
        shop = {'id': 1, 'platform': 'pdd'}
        normal = (shop, None, 'aftersales', {}, {'last_attempt': 0})
        urgent = (shop, None, 'aftersales', {'_attention': True}, {'last_attempt': 999, 'urgent_refund': True})
        chosen, deferred, _ = choose([normal, urgent], {}, policy(), 1000)
        self.assertEqual(chosen, [urgent])
        self.assertEqual(deferred, [normal])

    def test_queued_ordinary_scope_cannot_hold_urgent_representative_but_running_scope_can(self):
        shop = {'id': 1, 'platform': 'pdd'}
        urgent = (shop, None, 'aftersales', {'_attention': True}, {'urgent_refund': True, 'urgent_due': True})
        progress = {(1, 'ordinary'): job('aftersales')}
        chosen, _, _ = choose([urgent], progress, policy(max_concurrent=2), 1000)
        self.assertEqual(chosen, [urgent])
        progress[(1, 'urgent')] = self.urgent()
        self.assertEqual(select_jobs(progress, policy(max_concurrent=2), now=1000), [(1, 'urgent')])
        progress.pop((1, 'urgent'))
        progress[(1, 'ordinary')]['phase'] = 'running'
        chosen, deferred, _ = choose([urgent], progress, policy(max_concurrent=2), 1000)
        self.assertEqual(chosen, [])
        self.assertEqual(deferred, [urgent])

    def test_shop_turn_yields_at_reader_completion_not_by_cancelling_it(self):
        rotation = ShopRotation()
        tasks = {(1, 'active'): job('orders', phase='running'), (1, 'queued'): job('products'), (2, 'refund'): self.urgent()}
        config = policy(max_concurrent=2, max_active_shops=1, batch_seconds=60)
        rotation.reserve((1, 'active'), tasks, config=config, now=10)
        rotation.refresh(tasks, config=config, now=11)
        self.assertIn(1, rotation.yielding)
        self.assertEqual(tasks[(1, 'active')]['phase'], 'running')
        self.assertEqual(select_jobs(tasks, config, now=1000), [])
        tasks.pop((1, 'active'))
        rotation.refresh(tasks, config=config, now=12)
        self.assertNotIn(1, rotation.rounds)
        self.assertEqual(select_jobs(tasks, config, now=1000), [(2, 'refund')])


class StoredCasesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add('urgent', 'pw')
        self.shop = self.store.list()[0]
        self.service = BusinessService(self.store, lambda _: None)

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    def publish(self, rows, kind='aftersales'):
        attention.publish(self.store, self.sid, kind, {'rows': rows, 'complete': True})

    async def test_existing_platform_rows_precede_earlier_other_cases_before_paging(self):
        self.publish([{'id': str(i), 'status': '待举证', 'deadline': '2000-01-01'} for i in range(30)], 'evidence')
        self.publish([{**EXPECTED, 'id': '123', 'deadline': '2099-01-01'}])
        rows = attention.read(self.service)['rows']
        self.assertEqual(rows[0]['id'], '123')
        self.assertEqual(rows[0]['status'], '平台处理中')

    async def test_unknown_or_corrupt_intent_is_never_considered_new_work(self):
        self.publish([{**EXPECTED, 'id': '123'}])
        self.assertEqual(len(refund_priority.pending(self.store, self.sid)), 1)
        path = auto_refunds.record_path(self.store, self.sid, '123')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('damaged', encoding='utf-8')
        self.assertEqual(refund_priority.pending(self.store, self.sid), [])

    async def test_preflight_uses_native_guard_claims_once_and_skips_second_check(self):
        session = SimpleNamespace(shop=self.shop)
        self.publish([{**EXPECTED, 'id': '123'}])
        page = SimpleNamespace(open=AsyncMock())

        @asynccontextmanager
        async def borrow(*args):
            yield page

        native = AsyncMock(return_value={'list': [copy.deepcopy(CASE)]})
        submit = AsyncMock(side_effect=[copy.deepcopy(CHECK), {'success': True}])
        with patch('backend.read_page_pool.borrow', borrow), patch('backend.operations.uncached_read', native), patch('backend.auto_refunds.request', submit):
            checked = await auto_refunds.prioritize(session, self.store, 'aftersales', {'_attention': True})
            self.assertEqual(checked, {'123'})
            again = await auto_refunds.prioritize(session, self.store, 'aftersales', {'_attention': True})
            self.assertEqual(again, set())
        self.assertEqual(submit.await_count, 2)  # precheck + one mocked submission
        self.assertEqual(native.await_count, 1)
        record = json.loads(auto_refunds.record_path(self.store, self.sid, '123').read_text())
        self.assertEqual(record['state'], 'accepted')

    async def test_order_at_limit_is_still_never_submitted(self):
        session = SimpleNamespace(shop=self.shop)
        self.publish([{**EXPECTED, 'id': '123'}])
        page = SimpleNamespace(open=AsyncMock())

        @asynccontextmanager
        async def borrow(*args):
            yield page

        with patch('backend.read_page_pool.borrow', borrow), patch('backend.operations.uncached_read', AsyncMock(return_value={'list': [{**CASE, 'orderAmount': 10000}]})), patch('backend.auto_refunds.request', AsyncMock()) as submit:
            checked = await auto_refunds.prioritize(session, self.store, 'aftersales', {'_attention': True})
        self.assertEqual(checked, {'123'})
        submit.assert_not_awaited()
        self.assertFalse(refund_priority.needs_run(self.store, self.sid, refund_priority.pending(self.store, self.sid)))

    async def test_new_case_requests_one_turn_without_hot_loop_or_erasing_checkpoint(self):
        cases = [{**EXPECTED, 'id': '123'}]
        self.assertTrue(refund_priority.needs_run(self.store, self.sid, cases))
        refund_priority.checked(self.store, self.sid, cases, {'123'})
        self.assertFalse(refund_priority.needs_run(self.store, self.sid, cases))
        self.assertTrue(refund_priority.needs_run(self.store, self.sid, cases + [{**EXPECTED, 'id': '124'}]))

    async def test_receipt_write_failure_does_not_create_an_immediate_retry_loop(self):
        cases = [{**EXPECTED, 'id': '123'}]
        with patch('backend.collector_update.write_json', side_effect=PermissionError('fixture')):
            refund_priority.checked(self.store, self.sid, cases, {'123'})
        self.assertFalse(refund_priority.needs_run(self.store, self.sid, cases))

    async def test_new_case_is_admitted_before_next_cadence_without_force(self):
        config = assign(self.store, {'aftersales@attention': 'realtime'}, reset=True)
        self.service.configure_queue(config)
        self.publish([{**EXPECTED, 'id': '123'}])
        scope = next(o for k, o in attention.scopes(self.shop) if k == 'aftersales')
        key = self.service.key('aftersales', scope, self.shop)
        collection.register(self.store, self.sid, key, 'aftersales', scope, requested=False)
        collection.finish(self.store, self.sid, key)
        self.assertFalse(before_run(self.store, self.shop, 'aftersales', scope, key, self.service.key, subscribed=True, config=config))
        self.assertTrue(before_run(self.store, self.shop, 'aftersales', scope, key, self.service.key, subscribed=True, config=config, urgent=True))
        item = SimpleNamespace(shop=self.shop, state='logged_in', retired=False, visible=False, lock=asyncio.Lock(), control_until=0)
        self.service.session_getter = lambda _: item
        scheduler = Scheduler(self.service)
        await scheduler.tick_shop(self.shop, config)
        selected = [c for c in scheduler.candidates if c[2] == 'aftersales' and c[3].get('_attention')]
        self.assertEqual(len(selected), 1)
        self.assertTrue(selected[0][4]['urgent_due'])
        with patch.object(self.service, 'sync', AsyncMock()):
            plan = self.service.prepare_scheduled(selected, config)[0][3]
            self.service.admit(self.shop, item, 'aftersales', plan, True, False, False, urgent=True)
            meta = self.service.progress[(self.sid, key)]
            self.assertTrue(meta['urgent_due'])
            self.assertFalse(meta['force'])
            await asyncio.gather(*self.service.jobs.values())

    async def test_small_operation_budget_keeps_prior_decisions_until_recheck_is_due(self):
        self.publish([{**EXPECTED, 'id': '123'}, {**EXPECTED, 'id': '124'}])
        session = SimpleNamespace(shop=self.shop)
        page = SimpleNamespace(open=AsyncMock())

        @asynccontextmanager
        async def borrow(*args):
            yield page

        async def read(_, endpoint, body):
            return {'list': [{**CASE, 'id': int(body['id']), 'orderAmount': 10000}]}

        def budget(_):
            turns = iter([20, 0])
            return SimpleNamespace(remaining=lambda: next(turns))

        with patch('backend.read_page_pool.borrow', borrow), patch('backend.operations.uncached_read', AsyncMock(side_effect=read)) as native, patch('backend.auto_refunds.Budget', budget), patch('backend.auto_refunds.request', AsyncMock()) as submit:
            first = await auto_refunds.prioritize(session, self.store, 'aftersales', {'_attention': True})
            second = await auto_refunds.prioritize(session, self.store, 'aftersales', {'_attention': True})
            third = await auto_refunds.prioritize(session, self.store, 'aftersales', {'_attention': True})
        self.assertEqual((first, second, third), ({'123'}, {'124'}, set()))
        self.assertEqual(native.await_count, 2)
        submit.assert_not_awaited()
        self.assertFalse(refund_priority.needs_run(self.store, self.sid, refund_priority.pending(self.store, self.sid)))


class OperationBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_priority_operation_has_own_time_and_does_not_consume_read_budget(self):
        meta = {}
        async with ReadBudget(0.05, meta) as budget:
            with budget.operation():
                await asyncio.sleep(0.08)
            await asyncio.sleep(0.01)
            budget.finish()
        self.assertGreater(meta['priority_operation_seconds'], 0.07)
        self.assertLess(meta['read_seconds'], 0.05)
        self.assertNotIn('database_wait_seconds', meta)


if __name__ == '__main__':
    unittest.main()
