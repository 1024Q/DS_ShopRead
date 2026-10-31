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

from backend import attention, auto_refunds, refund_priority
from backend.store import Store
from backend.tests.test_auto_refunds import CASE, CHECK, EXPECTED


class RefundDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add('decision', 'pw', name='test')
        self.session = SimpleNamespace(shop=self.store.list()[0])
        attention.publish(self.store, self.sid, 'aftersales', {'rows': [EXPECTED], 'complete': True})
        self.native = patch('backend.operations.uncached_read', AsyncMock(return_value={'list': [{**CASE, 'afterSalesStatus': 2}]}))
        self.read = self.native.start()
        self.requests = patch('backend.auto_refunds.request', AsyncMock(side_effect=[copy.deepcopy(CHECK), {'success': True}]))
        self.request = self.requests.start()
        @asynccontextmanager
        async def borrow(*args):
            yield SimpleNamespace(open=AsyncMock())
        self.pages = patch('backend.read_page_pool.borrow', borrow)
        self.pages.start()

    async def asyncTearDown(self):
        self.native.stop(); self.requests.stop(); self.pages.stop()
        self.temp.cleanup()

    async def run_case(self):
        return await auto_refunds.prioritize(self.session, self.store, 'aftersales', {'_attention': True})

    def result(self):
        return refund_priority.receipt(self.store, self.sid)['results']['123']

    async def test_old_attempt_only_receipt_is_rechecked_and_native_status_two_submits_once(self):
        path = refund_priority.receipt_path(self.store, self.sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'checked': {'123': 'order-1'}, 'updated': time.time()}))
        self.assertTrue(refund_priority.needs_run(self.store, self.sid, refund_priority.pending(self.store, self.sid)))
        self.assertEqual(await self.run_case(), {'123'})
        self.assertEqual(self.result()['state'], 'accepted')
        intent = auto_refunds.load(auto_refunds.record_path(self.store, self.sid, 123))
        self.assertEqual(intent['state'], 'accepted')
        await self.run_case()
        self.assertEqual(self.request.await_count, 2)

    async def test_native_query_failure_is_not_checked_and_retry_is_due_after_backoff(self):
        self.read.side_effect = TimeoutError()
        self.assertEqual(await self.run_case(), set())
        result = self.result()
        receipt = refund_priority.receipt(self.store, self.sid)
        self.assertEqual(receipt['checked'], {})
        self.assertIn('原生查询未完成', result['reason'])
        cases = refund_priority.pending(self.store, self.sid)
        self.assertFalse(refund_priority.needs_run(self.store, self.sid, cases))
        with patch('backend.refund_priority.time.time', return_value=result['retry_at'] + 1):
            self.assertTrue(refund_priority.needs_run(self.store, self.sid, cases))
        self.request.assert_not_awaited()

    async def test_precheck_network_failure_is_not_checked_or_financially_claimed(self):
        self.request.side_effect = ConnectionError()
        self.assertEqual(await self.run_case(), set())
        self.assertEqual(refund_priority.receipt(self.store, self.sid)['checked'], {})
        self.assertIn('前置检查未完成', self.result()['reason'])
        self.assertFalse(auto_refunds.record_path(self.store, self.sid, 123).exists())

    async def test_skipped_large_order_records_reason_but_later_source_change_rechecks(self):
        self.read.return_value = {'list': [{**CASE, 'afterSalesStatus': 2, 'orderAmount': 10000}]}
        self.assertEqual(await self.run_case(), {'123'})
        self.assertIn('不小于100元', self.result()['reason'])
        cases = refund_priority.pending(self.store, self.sid)
        self.assertFalse(refund_priority.needs_run(self.store, self.sid, cases))
        self.assertTrue(refund_priority.needs_run(self.store, self.sid, [{**cases[0], 'progress': 'changed'}]))
        self.request.assert_not_awaited()

    async def test_risk_check_records_manual_reason_without_blocking_future_read_only_recheck(self):
        self.request.side_effect = [{**CHECK, 'result': {**CHECK['result'], 'tipSellerCheckGoods': True}}]
        self.assertEqual(await self.run_case(), {'123'})
        result = self.result()
        self.assertEqual(result['state'], 'manual')
        self.assertIn('先核验退回商品', result['reason'])
        self.assertIn('未提交退款', result['reason'])
        self.assertFalse(auto_refunds.record_path(self.store, self.sid, 123).exists())
        cases = refund_priority.pending(self.store, self.sid)
        with patch('backend.refund_priority.time.time', return_value=result['retry_at'] + 1):
            self.assertTrue(refund_priority.needs_run(self.store, self.sid, cases))
        self.assertEqual(self.request.await_count, 1)

    async def test_duplicate_or_missing_native_identity_is_never_submitted(self):
        for rows in ([], [CASE, CASE], [{**CASE, 'orderSn': 'other'}]):
            with self.subTest(rows=len(rows)):
                outcomes, completed = {}, set()
                self.read.return_value = {'list': rows}
                await auto_refunds.process(SimpleNamespace(), self.store, self.session.shop, [EXPECTED], attempted=completed, outcomes=outcomes)
                self.assertEqual(completed, {'123'})
                self.assertIn('唯一对应', outcomes['123']['reason'])
        self.request.assert_not_awaited()

    def test_malformed_risk_response_still_has_a_reason_without_changing_safety_rule(self):
        for envelope in (None, {'success': False}, {'success': True, 'result': []},
                         {'success': True, 'result': {'returnSceneRiskTipsVO': 'unexpected'}}):
            self.assertFalse(auto_refunds.precheck_safe(envelope))
            self.assertIn('未提交退款', auto_refunds.precheck_reason(envelope))

    async def test_claimed_unknown_remains_blocked_even_after_policy_receipt_invalidation(self):
        path = auto_refunds.record_path(self.store, self.sid, 123)
        auto_refunds.claim(path, {'state': 'unknown', 'case_id': '123', 'created_at': time.time()})
        self.assertEqual(await self.run_case(), set())
        self.read.assert_not_awaited()
        self.request.assert_not_awaited()

    async def test_cancelled_native_query_does_not_hide_case_as_checked(self):
        self.read.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.run_case()
        self.assertEqual(refund_priority.receipt(self.store, self.sid)['checked'], {})
        self.assertEqual(self.result()['state'], 'retry')
        self.request.assert_not_awaited()

    async def test_recent_receipt_survives_restart_without_repeating_native_requests(self):
        self.read.return_value = {'list': [{**CASE, 'orderAmount': 10000}]}
        await self.run_case()
        refund_priority._receipts.clear()
        self.assertEqual(await self.run_case(), set())
        self.assertEqual(self.read.await_count, 1)
        self.request.assert_not_awaited()
