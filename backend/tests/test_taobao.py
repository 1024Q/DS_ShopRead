from backend.frequency_policy import selected
import copy
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend.taobao import checked, fields, matches, normalize_trade, normalize_goods, period, SycmPage
from backend.platform_support import supports, source_page
from backend.business import BusinessService, options_for
from backend.store import Store
from backend.browser import Session
from backend.scheduler import Scheduler
from backend import collection


def live_fixture():
    return {
        "updateTime": "2026-10-09 12:31:00",
        "data": {
            "today": {
                "payAmt": {"value": 123.45},
                "payOrdCnt": {"value": 8},
                "payByrCnt": {"value": 6},
                "uv": {"value": 24},
                "payRate": {"value": 0.25},
                "rfdSucAmt": {"value": 4.5},
            },
            "yestday": {"payAmt": {"value": 240}, "rfdSucAmt": {"value": 15}},
        },
    }, {
        "data": {
            "today": {
                "statHour": ["00", "12", "13"],
                "payAmt": [0, 123.45, 999],
                "payOrdCnt": [0, 8, 99],
                "payByrCnt": [0, 6, 99],
            },
            "yesterday": {"statHour": ["00", "12", "23"], "payAmt": [0, 100, 240]},
        }
    }


class TaobaoNormalization(unittest.TestCase):
    def test_yuan_ratios_missing_and_zero(self):
        overview, trend = live_fixture()
        result = normalize_trade(overview, trend, {"day": "2026-10-09", "trade_range": "realtime"}, None)
        self.assertEqual(result["metrics"]["summary"]["sales"], 123.45)
        self.assertEqual(result["metrics"]["summary"]["conversion"], 25)
        self.assertEqual(result["metrics"]["summary"]["refund_amount"], 4.5)
        self.assertIsNone(result["metrics"]["summary"]["refund_orders"])
        today = [r for r in result["rows"] if r["series"] == "今日累计"]
        self.assertEqual([r["sales"] for r in today], [0, 123.45])
        self.assertEqual(result["metrics"]["comparisons"]["refund_amount"]["value"], 15)
        self.assertEqual(result["metrics"]["summary"]["buyers"], 6)  # never sum cumulative hours

    def test_reject_malformed_denied_stale(self):
        for body in ({}, {"code": 403, "data": {}}, {"hasError": True, "content": {"code": 0, "data": {}}}):
            with self.assertRaises(ValueError):
                checked(body)
        self.assertEqual(checked({"content": {"code": 0, "data": {"x": 1}}}), {"x": 1})
        overview, trend = live_fixture()
        with self.assertRaises(ValueError):
            normalize_trade(overview, trend, {"day": "2026-10-10", "trade_range": "realtime"}, None)
        overview["data"]["today"]["payAmt"]["value"] = True
        with self.assertRaises(ValueError):
            normalize_trade(overview, trend, {"day": "2026-10-09", "trade_range": "realtime"}, None)

    def test_period_and_history_ignore_competitors(self):
        options = {"day": "2026-10-09", "trade_range": "month"}
        self.assertEqual(period(options), ("2026-09-09", "2026-10-08"))
        self.assertTrue(matches({"dateRange": ["2026-09-09|2026-10-08,2026-08-10|2026-09-08"]}, options))
        self.assertFalse(matches({"dateRange": ["2026-09-09|2026-09-09"]}, options))
        overview = {
            "self": {"payAmt": {"value": 100}, "payOrdCnt": {"value": 10}, "payByrCnt": {"value": 8}},
            "rivalAvg": {"payAmt": {"value": 99999}},
        }
        trend = {
            "self": {"statDate": [1791388800000], "payAmt": [100]},
            "rivalAvg": {"statDate": [1791388800000], "payAmt": [99999]},
        }
        result = normalize_trade(overview, trend, options, "2026-09-09 至 2026-10-08")
        self.assertEqual(result["metrics"]["summary"]["sales"], 100)
        self.assertEqual(result["rows"][0]["sales"], 100)

    def test_goods_photo_no_false_orders_or_unique_totals(self):
        raw = {
            "item": {"itemId": "123", "title": "测试商品", "pictUrl": "//img.alicdn.com/test.jpg"},
            "payAmt": {"value": 99.99},
            "payItmCnt": {"value": 12},
            "itmUv": {"value": 8},
            "payRate": {"value": 0.5},
            "sucRefundAmt": {"value": 3.2, "cycleCrc": -0.1},
        }
        result = normalize_goods([raw], 2, {"day": "2026-10-09", "trade_range": "yesterday"}, "2026-10-08")
        row = result["rows"][0]
        self.assertEqual(row["thumbnail_url"], "https://img.alicdn.com/test.jpg")
        self.assertEqual(row["quantity"], 12)
        self.assertIsNone(row["orders"])
        self.assertEqual(row["refund_amount"], 3.2)
        self.assertEqual(row["comparison"]["refund_amount"]["value"], -10)
        self.assertIsNone(result["metrics"]["summary"]["visitors"])
        self.assertFalse(result["complete"])
        self.assertNotIn("created_date", row)

    def test_capabilities_and_navigation_are_per_module(self):
        for kind in ("overview", "transactions", "goodsdata"):
            self.assertTrue(supports("taobao", kind))
            self.assertIn("sycm.taobao.com", source_page("taobao", kind))
        for kind in ("products", "tickets"):
            self.assertFalse(supports("taobao", kind))
        for kind in ("tax", "packages", "aftersales", "invoices", "ad_products", "ad_reports"):
            self.assertTrue(supports("taobao", kind))
        self.assertTrue(supports("pdd", "products"))
        self.assertFalse(supports("unknown", "overview"))

    def test_export_new_metrics_use_chinese_labels(self):
        from backend.xlsx_export import table

        header = table([{"cart_quantity": 3, "refund_amount": 12.5, "scope_note": "支付子订单"}], "goodsdata")[0]
        self.assertIn("加购件数", header)
        self.assertIn("退款金额", header)
        self.assertIn("统计口径说明", header)


class TaobaoScopes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.pdd = self.store.add("pdd-fixture", "test")
        self.tb = self.store.add("tb-fixture", "test", platform="taobao")
        self.sessions = {s["id"]: Session(s, self.store) for s in self.store.list()}
        for s in self.sessions.values():
            s.state = "logged_in"
        self.service = BusinessService(self.store, self.sessions.__getitem__)

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    async def test_database_read_platform_isolation_and_no_jobs(self):
        for sid in self.sessions:
            shop = self.sessions[sid].shop
            key = self.service.key("overview", {}, shop)
            self.store.write_cache(sid, key, {"metrics": {"sales": sid}, "rows": [], "complete": True})
        self.assertEqual(
            [m["shop_id"] for m in self.service.data("overview", {}, platform="taobao")["metrics"]], [self.tb]
        )
        self.assertEqual(len(self.service.data("overview", {})["metrics"]), 2)
        self.assertEqual([s["shop_id"] for s in self.service.data("products", {})["sources"]], [self.pdd])
        self.assertFalse(self.service.jobs)

    async def test_scheduler_only_supported_kinds_with_daily_goods(self):
        from backend.tests.collection_fixture import assign

        config = assign(
            self.store,
            {
                "goodsdata@base": "realtime",
                "overview@base": "realtime",
                "goodsdata@yesterday": "history_yesterday",
                "tax@base": "daily",
                "aftersales@base": "custom",
            },
            platform="taobao",
            reset=True,
        )
        config["keepalive_enabled"] = False
        scheduler = Scheduler(self.service)
        shop = self.sessions[self.tb].shop
        await scheduler.tick_shop(shop, config)
        tb = [(kind, opts) for _, _, kind, opts, _ in scheduler.candidates]
        self.assertTrue(tb)
        self.assertTrue(all(supports("taobao", kind) for kind, _ in tb))
        self.assertIn("tax", {kind for kind, _ in tb})
        goods = [opts for kind, opts in tb if kind == "goodsdata"]
        self.assertEqual({g["trade_range"] for g in goods}, {"realtime", "yesterday"})
        self.assertEqual({selected("goodsdata", g, config, "taobao") for g in goods}, {"history_yesterday", "realtime"})

    async def test_changed_sycm_identity_cannot_save(self):
        session = self.sessions[self.tb]
        session.shop["detected_name"] = "店铺甲"
        page = SycmPage(session)
        page.wait_data = AsyncMock(return_value={"runAsShopTitle": "店铺乙", "runAsShopId": 123})
        with self.assertRaisesRegex(ValueError, "不一致"):
            await page.identity()
