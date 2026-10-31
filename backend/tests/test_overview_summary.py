import json
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend import overview_summary
from backend.business_reader import ReadPage
from backend.read_errors import PageReadError


def snapshot():
    return {
        "route_valid": True,
        "origin": 456,
        "source_time": "2026-10-28 00:10:00",
        "dom": {},
        "core": {
            "status": {"realTimeManagementData": 1, "bottomnums": 1},
            "showPromotion": True,
            "realtime": {
                "curPayOrdrAmt": 2356,
                "payOrdrAmtShow": 0,
                "curPayOrdrCnt": 1,
                "payOrdrCntShow": 0,
                "promotionSpend": 14820,
                "promotionSpendShow": 0,
                "guvOned": 12,
                "guvOnedShow": 0,
                "gpvOned": 14,
                "gpvOnedShow": 0,
                "goodsReviewCnt": 0,
                "goodsReviewCntShow": 0,
            },
            "bottom": {
                "waitPayNum": 3,
                "waitShipNum": 7,
                "pendingOrder": 2,
                "unShip12H": 0,
                "inqueryRate": 0.1944,
                "aftersalesNew": {"totalWaitHandleCount": 5, "expireEarlyWarningCount": 1},
            },
        },
    }


class OverviewMetricsTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required to execute the browser extraction fixture")
    def test_original_script_data_wins_over_trimmed_window_without_card_dom_scan(self):
        data = snapshot()
        core = data["core"]
        initial = {
            "props": {
                "pageProps": {
                    "coreData": {
                        "status": core["status"],
                        "data": {
                            "realTimeManagementData": {
                                "rtDataOverView": core["realtime"],
                                "showPromotion": core["showPromotion"],
                            },
                            "bottomnums": core["bottom"],
                        },
                    }
                },
                "user": {"private_fixture": "must-not-leave-page"},
            }
        }
        script = "global.window={__NEXT_DATA__:{props:{pageProps:{}}}};global.location={hostname:'mms.pinduoduo.com',pathname:'/home/'};"
        script += "const initial=" + json.dumps(initial) + ";"
        script += "global.document={getElementById:()=>({textContent:JSON.stringify(initial)}),querySelector:()=>({textContent:'实时数据更新时间: 2026-10-28 00:10:00'}),querySelectorAll:()=>{throw Error('complete structured data must not scan cards')}};"
        script += "process.stdout.write(JSON.stringify(" + overview_summary.SNAPSHOT_SCRIPT + "));"
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=True,
        )
        extracted = json.loads(result.stdout)
        self.assertEqual(overview_summary.metrics(extracted), overview_summary.metrics(data))
        self.assertEqual(extracted["dom"], {})
        self.assertNotIn("must-not-leave-page", result.stdout)

    def test_verified_native_units_and_all_card_fields(self):
        metrics = overview_summary.metrics(snapshot())
        self.assertEqual(
            metrics,
            {
                "sales": 23.56,
                "orders": 1,
                "ad_spend": 14.82,
                "visitors": 12,
                "views": 14,
                "reviews_today": 0,
                "pending_payment": 3,
                "pending_shipping": 7,
                "pending_aftersales": 5,
                "pending_tickets": 2,
                "shipping_due": 0,
                "aftersales_due": 1,
                "inquiry_conversion": 19.44,
                "source_time": "2026-10-28 00:10:00",
            },
        )

    def test_valid_zero_is_complete_and_does_not_use_dom_value(self):
        data = snapshot()
        for key in data["core"]["realtime"]:
            data["core"]["realtime"][key] = 0
        data["dom"] = {"sales": "99", "orders": "99"}
        metrics = overview_summary.metrics(data)
        self.assertEqual(metrics["sales"], 0)
        self.assertEqual(metrics["orders"], 0)

    def test_partial_fields_use_dom_without_inventing_zero(self):
        data = snapshot()
        del data["core"]["realtime"]["promotionSpend"]
        data["core"]["bottom"].pop("pendingOrder")
        data["core"]["bottom"]["waitPayNum"] = None
        data["dom"] = {"ad_spend": "1,234.56", "pending_payment": "0"}
        metrics = overview_summary.metrics(data)
        self.assertEqual(metrics["ad_spend"], 1234.56)
        self.assertEqual(metrics["pending_payment"], 0)
        self.assertIsNone(metrics["pending_tickets"])

    def test_missing_or_denied_sections_fall_back_to_visible_fields(self):
        data = snapshot()
        data["core"]["status"]["realTimeManagementData"] = 0
        data["dom"] = {"sales": "10.00", "orders": "0"}
        metrics = overview_summary.metrics(data)
        self.assertEqual(metrics["sales"], 10)
        self.assertEqual(metrics["orders"], 0)
        self.assertIsNone(metrics["ad_spend"])
        data = snapshot()
        data["core"]["showPromotion"] = False
        self.assertIsNone(overview_summary.metrics(data)["ad_spend"])
        data["core"]["realtime"]["payOrdrAmtShow"] = 1
        with self.assertRaises(ValueError):
            overview_summary.metrics(data)

    def test_malformed_optional_numbers_are_missing_not_zero(self):
        for value in (None, True, False, -1, "0", float("nan"), float("inf"), {}):
            data = snapshot()
            data["core"]["realtime"]["promotionSpend"] = value
            self.assertIsNone(overview_summary.metrics(data)["ad_spend"])

    def test_native_money_rounding_matches_javascript_display(self):
        # The native card divides and calls Number.toFixed(2), not decimal half-even.
        self.assertEqual(overview_summary.display_round(1005 / 1000), 1.0)
        self.assertEqual(overview_summary.display_round(125 / 1000), 0.13)
        self.assertEqual(overview_summary.display_round(2675 / 1000), 2.67)

    def test_missing_bootstrap_still_supports_existing_dom_statistics(self):
        data = {"route_valid": True, "core": {}, "dom": {"sales": "1,234.50", "orders": "10"}, "source_time": None}
        metrics = overview_summary.metrics(data)
        self.assertEqual(metrics["sales"], 1234.5)
        self.assertIsNone(metrics["pending_payment"])
        self.assertIsNone(metrics["source_time"])

    def test_different_page_never_looks_like_empty_success(self):
        with self.assertRaises(PageReadError):
            overview_summary.metrics({"route_valid": False})


class OverviewReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_snapshot_uses_one_compact_browser_read(self):
        page = SimpleNamespace(overview_origin=456, evaluate=AsyncMock(return_value=snapshot()))
        result = await overview_summary.collect(page)
        self.assertTrue(result["complete"])
        page.evaluate.assert_awaited_once()
        self.assertNotIn("document.body.innerText", page.evaluate.await_args.args[0])

    async def test_changed_document_is_rejected_before_publication(self):
        page = SimpleNamespace(overview_origin=123, evaluate=AsyncMock(return_value=snapshot()))
        with self.assertRaisesRegex(PageReadError, "文档已切换"):
            await overview_summary.collect(page)

    async def test_reused_overview_reloads_and_records_new_document_identity(self):
        page = ReadPage(SimpleNamespace())
        page.reused_kind = "overview"
        page.call = AsyncMock()
        page.evaluate = AsyncMock(side_effect=[123, {"ready": True, "login": False, "origin": 456}])
        with patch("backend.business_reader.asyncio.sleep", AsyncMock()):
            await page.open("overview")
        self.assertEqual(page.overview_origin, 456)
        self.assertIn("Page.reload", [call.args[0] for call in page.call.await_args_list])
        self.assertIn("performance.timeOrigin!==123", page.evaluate.await_args_list[-1].args[0])

    async def test_late_dom_retries_only_until_required_fields_available(self):
        page = SimpleNamespace(
            evaluate=AsyncMock(side_effect=[{"route_valid": True, "core": {}, "dom": {}}, snapshot()])
        )
        with patch("backend.overview_summary.asyncio.sleep", AsyncMock()):
            result = await overview_summary.collect(page)
        self.assertEqual(result["metrics"]["sales"], 23.56)
        self.assertEqual(page.evaluate.await_count, 2)
