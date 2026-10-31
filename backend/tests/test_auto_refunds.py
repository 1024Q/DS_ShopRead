import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend import auto_refunds as refunds

CASE = {"id": 123, "orderSn": "order-1", "uid": None, "orderAmount": 727, "refundAmount": 727,
        "afterSalesStatus": 3, "afterSalesTitle": "退款，拼多多处理中", "actions": [1001], "version": 4}
EXPECTED = {"id": "123", "order_number": "order-1", "status": "平台处理中"}
CHECK = {"success": True, "result": {"tipSellerCheckGoods": False, "tipSellerAccountAbnormal": False,
                                    "returnSceneRiskTipsVO": {"hasRisk": False, "riskTips": None}}}


class PolicyTests(unittest.TestCase):
    def test_strict_order_amount_and_identity(self):
        self.assertTrue(refunds.eligible(CASE, EXPECTED))
        for change in (
            {"orderAmount": 10000}, {"orderAmount": 10001}, {"orderAmount": None},
            {"orderAmount": "727"}, {"orderAmount": 727.0}, {"orderAmount": True},
            {"orderAmount": 0}, {"orderAmount": -1}, {"refundAmount": 728},
            {"afterSalesStatus": 7}, {"afterSalesStatus": True}, {"afterSalesStatus": "2"},
            {"afterSalesTitle": "退款，待卖家处理"},
            {"actions": []}, {"version": None}, {"orderSn": "other"}, {"id": 124},
        ):
            with self.subTest(change=change):
                self.assertFalse(refunds.eligible({**CASE, **change}, EXPECTED))
        self.assertTrue(refunds.eligible({**CASE, "orderAmount": 9999}, EXPECTED))
        for status in (2, 3):
            for title in ('退款，拼多多处理中', '退货退款，拼多多处理中'):
                self.assertTrue(refunds.eligible({**CASE, 'afterSalesStatus': status, 'afterSalesTitle': title}, EXPECTED))

    def test_native_risk_checks_fail_closed(self):
        self.assertTrue(refunds.precheck_safe(CHECK))
        for change in (
            {"tipSellerCheckGoods": True}, {"tipSellerCheckGoods": None},
            {"tipSellerAccountAbnormal": True}, {"showAgreeRefundDoubleCheckBox": True},
            {"sellerAccountShortAmount": 1}, {"unknownRisk": False},
            {"returnSceneRiskTipsVO": {"hasRisk": True}},
            {"returnSceneRiskTipsVO": {"riskTips": None}},
        ):
            self.assertFalse(refunds.precheck_safe({"success": True, "result": {**CHECK["result"], **change}}))
        for envelope in ({}, {"success": False}, {"success": True, "result": {}}, None):
            self.assertFalse(refunds.precheck_safe(envelope))


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = SimpleNamespace(directory=Path(self.folder.name))
        self.shop = {"id": 21, "name": "test", "platform": "pdd"}
        self.page = SimpleNamespace()
        self.native = patch("backend.operations.uncached_read", AsyncMock(return_value={"list": [copy.deepcopy(CASE)]}))
        self.read = self.native.start()
        self.fetcher = patch("backend.auto_refunds.request", AsyncMock(side_effect=[copy.deepcopy(CHECK), {"success": True}]))
        self.fetch = self.fetcher.start()

    async def asyncTearDown(self):
        self.native.stop(); self.fetcher.stop(); self.folder.cleanup()

    async def run_case(self):
        return await refunds.process(self.page, self.store, self.shop, [EXPECTED])

    async def test_accepted_once_records_integer_amounts_before_submission(self):
        async def submit(page, path, body):
            if path == refunds.PRECHECK:
                return CHECK
            intent = json.loads(refunds.record_path(self.store, 21, 123).read_text(encoding="utf-8"))
            self.assertEqual(intent["state"], "submitting")
            self.assertEqual(body, {"operateDesc": "商家同意退款", "uid": None, "id": 123, "orderSn": "order-1", "version": 4})
            return {"success": True}
        self.fetch.side_effect = submit
        changed, records = await self.run_case()
        self.assertTrue(changed)
        self.assertEqual(records[0]["state"], "accepted")
        self.assertEqual(records[0]["order_amount_cents"], 727)
        await self.run_case()
        self.assertEqual(self.fetch.await_count, 2)
        self.assertEqual(refunds.records(self.store, 21)["rows"][0]["state"], "accepted")

    async def test_unknown_network_result_is_not_retried(self):
        self.fetch.side_effect = [CHECK, TimeoutError()]
        changed, records = await self.run_case()
        self.assertFalse(changed)
        self.assertEqual(records[0]["state"], "unknown")
        await self.run_case()
        self.assertEqual(self.fetch.await_count, 2)

    async def test_cancellation_preserves_uncertainty(self):
        self.fetch.side_effect = [CHECK, asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await self.run_case()
        self.assertEqual(refunds.records(self.store, 21)["rows"][0]["state"], "unknown")
        await self.run_case()
        self.assertEqual(self.fetch.await_count, 2)

    async def test_precheck_risk_does_not_submit(self):
        self.fetch.side_effect = [{"success": True, "result": {**CHECK["result"], "tipSellerCheckGoods": True}}]
        changed, records = await self.run_case()
        self.assertFalse(changed)
        self.assertEqual(records[0]["state"], "manual")
        self.assertEqual(self.fetch.await_count, 1)

    async def test_failed_audit_write_prevents_submission(self):
        with patch("backend.auto_refunds.claim", side_effect=PermissionError()):
            with self.assertRaises(PermissionError):
                await self.run_case()
        self.assertEqual(self.fetch.await_count, 1)

    async def test_changed_status_or_large_order_does_not_submit(self):
        for change in ({"afterSalesTitle": "退款，待卖家处理"}, {"orderAmount": 10000}):
            self.read.return_value = {"list": [{**CASE, **change}]}
            self.assertEqual(await self.run_case(), (False, []))
        self.fetch.assert_not_awaited()

    async def test_platform_rejection_not_blindly_retried(self):
        self.fetch.side_effect = [CHECK, {"success": False, "errorCode": 400}]
        changed, records = await self.run_case()
        self.assertFalse(changed)
        self.assertEqual(records[0]["state"], "rejected")
        await self.run_case()
        self.assertEqual(self.fetch.await_count, 2)

    async def test_ordinary_views_and_other_platforms_are_read_only(self):
        for kind, options, platform in (("aftersales", {}, "pdd"), ("orders", {"_attention": True}, "pdd"),
                                        ("aftersales", {"_attention": True}, "jd")):
            data = {"complete": True, "rows": [EXPECTED]}
            result, changed = await refunds.apply(
                SimpleNamespace(shop={"platform": platform}), self.store, kind, options, data
            )
            self.assertIs(result, data)
            self.assertFalse(changed)
        self.fetch.assert_not_awaited()

    async def test_atomic_claim_prevents_competing_submitters(self):
        path = refunds.record_path(self.store, 21, 123)
        self.assertTrue(refunds.claim(path, {"state": "submitting"}))
        self.assertFalse(refunds.claim(path, {"state": "submitting"}))
        await self.run_case()
        self.fetch.assert_not_awaited()


class RecordToleranceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = SimpleNamespace(directory=Path(self.folder.name))
        self.shop = {"id": 21, "name": "test", "platform": "pdd"}

    async def asyncTearDown(self):
        self.folder.cleanup()

    def write(self, case, text):
        path = refunds.record_path(self.store, 21, case)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    async def test_corrupt_or_empty_record_is_isolated_and_never_resubmitted(self):
        self.write(123, "")
        self.write(124, "{not json")
        good = {"case_id": "125", "state": "accepted", "created_at": 5}
        self.write(125, json.dumps(good))
        listed = refunds.records(self.store, 21)
        states = {row["case_id"]: row["state"] for row in listed["rows"]}
        self.assertEqual(states, {"123": "review", "124": "review", "125": "accepted"})
        self.assertEqual(listed["pending_review"], 2)
        bad = list((refunds.record_path(self.store, 21, 1).parent / "quarantine").glob("*.bad"))
        self.assertEqual(len(bad), 2)
        with (
            patch("backend.operations.uncached_read", AsyncMock(return_value={"list": [copy.deepcopy(CASE)]})) as read,
            patch("backend.auto_refunds.request", AsyncMock()) as submit,
        ):
            self.assertEqual((await refunds.process(SimpleNamespace(), self.store, self.shop, [EXPECTED]))[1][0]["state"], "review")
        read.assert_not_awaited()
        submit.assert_not_awaited()

    async def unknown(self, age=600):
        record = {"case_id": "123", "order_number": "order-1", "state": "unknown", "version": 4,
                  "created_at": __import__("time").time() - age}
        path = self.write(123, json.dumps(record))
        return path

    async def test_unknown_result_is_reconciled_read_only(self):
        await self.unknown()
        for listed, expected in (([], "unknown"), ([copy.deepcopy(CASE)], "not_applied"),
                                 ([{**CASE, "afterSalesTitle": "退款成功", "version": 5}], "accepted")):
            await self.unknown()
            with (
                patch("backend.operations.uncached_read", AsyncMock(return_value={"list": listed})),
                patch("backend.auto_refunds.request", AsyncMock()) as submit,
            ):
                checked = await refunds.reconcile(SimpleNamespace(), self.store, self.shop)
            submit.assert_not_awaited()
            self.assertEqual(checked[0]["state"], expected)
            self.assertEqual(refunds.records(self.store, 21)["rows"][0]["state"], expected)

    async def test_recent_or_recently_verified_unknown_waits(self):
        await self.unknown(age=10)
        with patch("backend.operations.uncached_read", AsyncMock()) as read:
            self.assertEqual(await refunds.reconcile(SimpleNamespace(), self.store, self.shop), [])
        read.assert_not_awaited()

    async def test_financial_record_wins_over_a_later_verification_retry(self):
        from backend import refund_priority
        await self.unknown()
        refund_priority.deferred(self.store, 21, [EXPECTED], 'page retry')
        result = refunds.records(self.store, 21)
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['rows'][0]['state'], 'unknown')
        self.assertEqual(result['rows'][0]['record_type'], 'financial')
        self.assertEqual(refunds.records(self.store, 21, state='retry')['rows'], [])

    async def test_verification_filters_and_cursor_include_skipped_and_manual_reasons(self):
        from backend import refund_priority
        other = {**EXPECTED, 'id':'124'}
        refund_priority.checked(self.store, 21, [EXPECTED, other], {'123', '124'}, {
            '123': {'state':'skipped','reason':'订单金额不小于100元','checked_at':8,'retry_at':10},
            '124': {'state':'manual','reason':'平台要求验货','checked_at':9,'retry_at':10},
        })
        first = refunds.records(self.store, 21, limit=1)
        self.assertEqual(first['rows'][0]['case_id'], '124')
        self.assertEqual(first['pending_review'], 1)
        second = refunds.records(self.store, 21, limit=1, before=first['next_before'], before_case_id=first['next_case_id'])
        self.assertEqual(second['rows'][0]['case_id'], '123')
        self.assertEqual(refunds.records(self.store, 21, state='manual')['rows'][0]['reason'], '平台要求验货')
        self.assertFalse(refunds.record_path(self.store, 21, '123').exists())
        await self.unknown()
        with patch("backend.operations.uncached_read", AsyncMock(side_effect=TimeoutError())):
            checked = await refunds.reconcile(SimpleNamespace(), self.store, self.shop)
        self.assertEqual(checked[0]["state"], "unknown")
        with patch("backend.operations.uncached_read", AsyncMock()) as read:
            self.assertEqual(await refunds.reconcile(SimpleNamespace(), self.store, self.shop), [])
        read.assert_not_awaited()

    async def test_refund_budget_timeout_keeps_saved_payload(self):
        data = {"complete": True, "rows": [EXPECTED]}

        async def slow(*args, **kwargs):
            await asyncio.sleep(5)

        session = SimpleNamespace(shop={"platform": "pdd", "id": 21})
        with patch("backend.read_page_pool.borrow", side_effect=lambda *a: SlowPage(slow)):
            result, changed = await refunds.apply(session, self.store, "aftersales", {"_attention": True}, data, seconds=0.05)
        self.assertIs(result, data)
        self.assertFalse(changed)


class SlowPage:
    def __init__(self, slow):
        self.slow = slow

    async def __aenter__(self):
        await self.slow()

    async def __aexit__(self, *args):
        return False


if __name__ == "__main__":
    unittest.main()
