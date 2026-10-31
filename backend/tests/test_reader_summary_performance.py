import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend import promotions, transactions, summary_publication
from backend.business_reader import ReadPage
from backend.read_errors import PageReadError


def promotion_summary():
    return {
        "sumReport": {
            "orderSpend": {"value": "12.34", "unit": "YUAN"},
            "spend": {"value": "15.67", "unit": "YUAN"},
            "gmv": {"value": "100", "unit": "YUAN"},
        },
        "hourlyReportList": [],
        "reportLatestUpdateTime": 1793114886000,
    }


def trade_core():
    return {
        "getMallTradeInfo": {"payOrdrAmt": "100", "payOrdrCnt": "4", "payOrdrUsrCnt": "3"},
        "queryMallTradeList": {"todayRtList": [{"hr": "10", "payOrdrAmt": "100"}]},
    }


class PromotionResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_latest_update_time_and_legacy_names_are_compatible(self):
        for key in ("reportLatestUpdateTime", "reportLastUpdateTime", "lastUpdateTime"):
            self.assertEqual(promotions.report_time({key: 1793114886000}), "2026-10-27 23:28:06")
        self.assertEqual(
            promotions.report_time({"reportLatestUpdateTime": "2026-10-27T15:28:06Z"}), "2026-10-27 23:28:06"
        )
        self.assertIsNone(promotions.report_time({"reportLatestUpdateTime": "unknown"}))

    async def test_reused_promotion_page_reloads_without_creating_or_navigating_target(self):
        page = promotions.PromotionPage(SimpleNamespace())
        page.reused_kind = "ad_reports"
        page.call = AsyncMock()
        page.select = AsyncMock(return_value=True)
        page.evaluate = AsyncMock(side_effect=[123, {"ready": True, "login": False}])
        await page.open_promotion("ad_reports", promotions.options_for())
        methods = [call.args[0] for call in page.call.await_args_list]
        self.assertIn("Page.reload", methods)
        self.assertNotIn("Page.navigate", methods)
        self.assertIn("performance.timeOrigin!==123", page.evaluate.await_args_list[-1].args[0])
        self.assertIn("yingxiao.pinduoduo.com", page.reuse_ready_expression("ad_reports"))

    async def test_new_summary_endpoint_matches_exact_dates_and_legacy_still_works(self):
        page = promotions.PromotionPage(SimpleNamespace())
        options = {"trade_range": "realtime", "date_from": "2026-10-27", "date_to": "2026-10-27"}
        stale = {"beginDate": "2026-10-26", "endDate": "2026-10-26"}
        current = {"beginDate": "20261027", "endDate": "2026-10-27T00:00:00+08:00"}
        page.events = [
            (promotions.RANGE_SUMMARY, stale, {"success": True, "result": {"wrong": True}}),
            (promotions.RANGE_SUMMARY, current, {"success": True, "result": promotion_summary()}),
        ]
        result, scope = await page.wait(promotions.SUMMARY, options)
        self.assertEqual(result["sumReport"]["gmv"]["value"], "100")
        self.assertEqual(scope, current)
        self.assertEqual(len(page.events), 1)
        page.events = [(promotions.SUMMARY, current, {"success": True, "result": promotion_summary()})]
        self.assertEqual((await page.wait(promotions.SUMMARY, options))[0], promotion_summary())

    async def test_http_error_is_reported_immediately_with_matching_scope(self):
        page = promotions.PromotionPage(SimpleNamespace())
        options = promotions.options_for()
        scope = {"beginDate": options["date_from"], "endDate": options["date_to"]}

        async def messages():
            yield json.dumps(
                {
                    "method": "Network.requestWillBeSent",
                    "params": {
                        "requestId": "r1",
                        "request": {
                            "url": "https://yingxiao.pinduoduo.com" + promotions.RANGE_SUMMARY,
                            "postData": json.dumps(scope),
                        },
                    },
                }
            )
            yield json.dumps(
                {"method": "Network.responseReceived", "params": {"requestId": "r1", "response": {"status": 403}}}
            )

        page.ws = messages()
        await page.receive()
        with self.assertRaisesRegex((ValueError, PageReadError), "HTTP 403"):
            await asyncio.wait_for(page.wait(promotions.SUMMARY, options), 0.2)
        self.assertFalse(page.ad_requests)

    async def test_delayed_body_from_previous_use_cannot_satisfy_new_read(self):
        page = promotions.PromotionPage(SimpleNamespace())
        started = asyncio.Event()
        finish = asyncio.Event()

        async def response(*args):
            started.set()
            await finish.wait()
            return {"body": json.dumps({"success": True, "result": promotion_summary()})}

        page.call = response
        task = asyncio.create_task(page.read_response("old", (promotions.SUMMARY, {}, page.response_generation)))
        await started.wait()
        page.reset_buffers()
        finish.set()
        await task
        self.assertFalse(page.events)


class SummaryPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_promotion_core_does_not_publish_or_read_details(self):
        page = SimpleNamespace(open_promotion=AsyncMock(), wait=AsyncMock(return_value=({"sumReport": {}}, {})))

        @asynccontextmanager
        async def borrow(*args):
            yield page

        published = AsyncMock()
        details = AsyncMock()
        with (
            patch("backend.read_page_pool.borrow", borrow),
            patch("backend.promotions.read_rows", details),
            summary_publication.context(published),
        ):
            with self.assertRaisesRegex(ValueError, "完整金额"):
                await promotions.collect(SimpleNamespace(), "ad_reports", promotions.options_for())
        published.assert_not_awaited()
        details.assert_not_awaited()

    async def test_promotion_summary_survives_subsequent_detail_failure(self):
        page = SimpleNamespace(open_promotion=AsyncMock(), wait=AsyncMock(return_value=(promotion_summary(), {})))

        @asynccontextmanager
        async def borrow(*args):
            yield page

        published = AsyncMock()
        with (
            patch("backend.read_page_pool.borrow", borrow),
            patch("backend.promotions.read_rows", AsyncMock(side_effect=PageReadError("明细超时"))),
            summary_publication.context(published),
        ):
            with self.assertRaisesRegex(PageReadError, "明细超时"):
                await promotions.collect(SimpleNamespace(), "ad_reports", promotions.options_for())
        published.assert_awaited_once()
        kind, options, payload = published.await_args.args
        self.assertEqual(kind, "ad_reports")
        self.assertEqual(payload["metrics"]["total_spend"], 15.67)
        self.assertEqual(payload["metrics"]["source_time"], "2026-10-27 23:28:06")
        self.assertTrue(payload["summary_complete"])
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["rows"], [])

    async def test_transactions_publish_core_before_auxiliary_failure_without_erasing_old_widgets(self):
        published = AsyncMock()
        wait = AsyncMock(side_effect=[trade_core(), PageReadError("地域数据超时")])
        with (
            patch("backend.transactions.wait_responses", wait),
            patch("backend.transactions.display_map", AsyncMock(return_value={})),
            patch("backend.transactions.source_time", AsyncMock(return_value="2026-10-27 12:00:00")),
            summary_publication.context(published),
        ):
            with self.assertRaisesRegex(PageReadError, "地域数据超时"):
                await transactions.collect(SimpleNamespace(), {"trade_range": "realtime", "day": "2026-10-27"})
        self.assertEqual(wait.await_args_list[0].args[1], transactions.CORE_ENDPOINTS)
        payload = published.await_args.args[2]
        self.assertEqual(payload["metrics"]["summary"]["sales"], 100)
        self.assertEqual(len(payload["rows"]), 1)
        self.assertNotIn("annual", payload["metrics"])
        self.assertNotIn("regions", payload["metrics"])
        self.assertTrue(payload["summary_complete"])
        self.assertFalse(payload["complete"])

    async def test_transactions_full_completion_still_includes_auxiliary_widgets(self):
        aux = {name: {} for name in transactions.ENDPOINTS - transactions.CORE_ENDPOINTS}
        aux["getMallNotPayOrderInfoV2"] = {"notPayOrderCnt": 2, "notPayOrderAmountCnt": 500}
        wait = AsyncMock(side_effect=[trade_core(), aux])
        published = AsyncMock()
        with (
            patch("backend.transactions.wait_responses", wait),
            patch("backend.transactions.display_map", AsyncMock(return_value={})),
            patch("backend.transactions.source_time", AsyncMock(return_value=None)),
            summary_publication.context(published),
        ):
            result = await transactions.collect(SimpleNamespace(), {"trade_range": "realtime", "day": "2026-10-27"})
        self.assertTrue(result["complete"])
        self.assertEqual(result["metrics"]["orders_info"]["pending_orders"], 2)
        self.assertEqual(result["metrics"]["orders_info"]["pending_amount"], 5)
        published.assert_awaited_once()


class FreshTradeResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_product_body_cannot_seed_refreshed_page(self):
        page = ReadPage(SimpleNamespace())
        started = asyncio.Event()
        finish = asyncio.Event()

        async def response(*args):
            started.set()
            await finish.wait()
            return {"body": json.dumps({"success": True, "result": {"goods_list": [], "total": 0}})}

        page.call = response
        task = asyncio.create_task(page.read_products("old", 0, {"page": 1}))
        await started.wait()
        page.reset_buffers()
        finish.set()
        await task
        self.assertTrue(page.products.empty())

    async def test_reused_product_page_refreshes_initial_list_before_reader_waits(self):
        page = ReadPage(SimpleNamespace())
        page.reused_kind = "products"
        page.call = AsyncMock()
        page.evaluate = AsyncMock(side_effect=[123, {"ready": True, "login": False}])
        with patch("backend.business_reader.asyncio.sleep", AsyncMock()):
            await page.open("products")
        self.assertIn("Page.reload", [call.args[0] for call in page.call.await_args_list])
        self.assertIn("performance.timeOrigin!==123", page.evaluate.await_args_list[-1].args[0])

    async def test_old_range_and_previous_page_generation_are_ignored(self):
        page = ReadPage(SimpleNamespace())
        name = "getMallTradeInfo"
        page.reset_trade((name,))
        page.trade_result(name, 0, 0, {"success": True, "result": {"old": True}})
        self.assertNotIn(name, page.trade_responses)
        page.trade_result(name, 0, 1, {"success": True, "result": {"fresh": True}})
        self.assertTrue(page.trade_changed.is_set())
        self.assertEqual((await transactions.wait_responses(page, (name,)))[name], {"fresh": True})
        page.reset_buffers()
        page.trade_result(name, 0, 1, {"success": True, "result": {"old": True}})
        self.assertNotIn(name, page.trade_responses)

    async def test_response_signal_wakes_wait_without_polling_delay(self):
        page = ReadPage(SimpleNamespace())
        page.evaluate = AsyncMock(return_value=False)
        task = asyncio.create_task(transactions.wait_responses(page, transactions.CORE_ENDPOINTS))
        await asyncio.sleep(0)
        for name, result in trade_core().items():
            page.trade_result(name, 0, 0, {"success": True, "result": result})
        self.assertEqual(await asyncio.wait_for(task, 0.2), trade_core())

    async def test_single_failed_core_response_does_not_wait_for_other_endpoint(self):
        page = ReadPage(SimpleNamespace())
        page.trade_result("getMallTradeInfo", 0, 0, {"success": False, "errorMsg": "数据接口 HTTP 403"})
        with self.assertRaisesRegex(ValueError, "HTTP 403"):
            await asyncio.wait_for(transactions.wait_responses(page, transactions.CORE_ENDPOINTS), 0.2)
