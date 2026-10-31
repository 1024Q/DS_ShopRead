import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from backend.taobao_flow import normalize
from backend.taobao_operations import package, invoice, checked as operation_checked
from backend.taobao_ads import fields, campaign, checked, matches, identity
from backend.taobao_page import TaobaoPage
from backend.operations import decorate


class ModuleNormalization(unittest.TestCase):
    def test_package_keeps_order_waybill_photo_without_buyer_pii(self):
        raw = {
            "id": "waybill#exception",
            "tradeId": "3316904149194034965",
            "mailNo": "773444898716861",
            "eventStatus": 2,
            "eventStatusName": "运输停滞",
            "estimateClaimAmount": "5.00",
            "receiver": {"telphone": "private"},
            "buyerNick": "private",
            "sellerId": 123,
            "subTradeOrderList": [{"itemId": "456", "goodsName": "测试商品", "iconUrl": "//img.alicdn.com/a.jpg"}],
            "consignTime": "2026-10-05 08:51:39",
            "gmtCreate": "2026-10-09 17:39:04",
        }
        row = decorate("aftersales", package(raw, "运输更新异常"))
        self.assertEqual(row["estimated_claim"], 5)
        self.assertIsNone(row["amount"])
        self.assertEqual(row["priority"], 0)
        self.assertEqual(row["order_number"], raw["tradeId"])
        self.assertEqual(row["thumbnail_url"], "https://img.alicdn.com/a.jpg")
        self.assertFalse(set(row) & {"receiver", "buyerNick", "sellerId"})
        self.assertEqual(row["created_at"], raw["consignTime"])

    def test_invoice_is_yuan_and_pending_status_is_searchable(self):
        row = invoice(
            {
                "serialNo": "a",
                "tid": "1234567890123456789",
                "amount": "239.40",
                "payerName": "企业",
                "payerBankAccount": "private",
            },
            "待录入开票",
        )
        self.assertEqual(row["amount"], 239.4)
        self.assertEqual(row["status"], "待开票")
        self.assertNotIn("payerBankAccount", row)
        with self.assertRaises(ValueError):
            operation_checked({"code": 403, "data": []}, True)

    def test_ads_roi_and_percent_preserve_missing_total_charge(self):
        r = fields(
            {
                "charge": 50,
                "alipayInshopAmt": 150,
                "retainedAlipayInshopAmt": 100,
                "ctr": 0.038,
                "cvr": 0.12,
                "roi": 999,
            }
        )
        self.assertEqual(r["roi"], 3)
        self.assertEqual(r["net_roi"], 2)
        self.assertAlmostEqual(r["ctr"], 3.8)
        self.assertEqual(r["cvr"], 12)
        self.assertIsNone(r["total_spend"])
        self.assertIsNone(r["settlement_revenue"])
        self.assertIsNone(fields({"charge": 0, "alipayInshopAmt": 100})["roi"])
        with self.assertRaises(ValueError):
            checked({"info": {"ok": False}, "data": []})

    def test_campaign_units_scope_and_thumbnail(self):
        options = {"date_from": "2026-10-09", "date_to": "2026-10-09"}
        raw = {
            "campaignId": 1,
            "campaignName": "计划甲",
            "dayBudget": 500,
            "constraintValue": 3.3,
            "displayStatus": "start",
            "adgroupList": [
                {"material": {"materialId": 123, "title": "商品甲", "imageUrl": "//img.alicdn.com/item.jpg"}}
            ],
            "reportInfoList": [
                {
                    "condition": {"startTime": options["date_from"], "endTime": options["date_to"]},
                    "charge": 20,
                    "alipayInshopAmt": 80,
                }
            ],
        }
        r = campaign(raw, options)
        self.assertEqual(r["budget"], 500)
        self.assertEqual(r["roi"], 4)
        self.assertEqual(r["goods_id"], "123")
        self.assertTrue(r["thumbnail_url"].startswith("https://"))
        with self.assertRaises(ValueError):
            campaign(raw, {**options, "date_to": "2026-10-08"})

    def test_flow_hourly_not_cumulative_and_historical_cross_year(self):
        r = normalize(
            {"updateTime": "2026-10-09 01:20:00", "data": {"uv": {"value": 8}, "itmUv": {"value": 6}}},
            {"data": {"today": {"uv": [3, 5, 999]}, "yesterday": {"uv": [7, 6]}}},
            [],
            {},
            {"trade_range": "realtime", "day": "2026-10-09"},
        )
        self.assertEqual(r["summary"]["shop_visitors"], 8)
        self.assertEqual([x["shop_visitors"] for x in r["trend"] if x["series"] == "今日分时"], [3, 5])
        r = normalize(
            {"uv": {"value": 8}, "itmUv": {"value": 6}},
            {"my": {"statDate": ["12-31", "01-01"], "uv": [3, 5]}, "rivalAvg": {"uv": [999, 999]}},
            [{"pageName": {"value": "渠道甲"}, "uv": {"value": 8}, "children": [{"uv": 999}]}],
            {},
            {"trade_range": "week", "day": "2027-01-02"},
        )
        self.assertEqual([x["date"] for x in r["trend"]], ["2026-12-31", "2027-01-01"])
        self.assertEqual(len(r["channels"]), 1)
        self.assertEqual(r["channels"][0]["visitors"], 8)

    def test_observer_ignores_preflight_and_authentication_fields(self):
        p = TaobaoPage(None, {"one.alimama.com": {"/report/query.json"}})
        event = lambda method: {
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "1",
                "request": {
                    "url": "https://one.alimama.com/report/query.json?token=secret",
                    "method": method,
                    "postData": '{"csrfId":"secret","queryDomains":["account"],"startTime":"2026-10-09"}',
                },
            },
        }
        p.observe(event("OPTIONS"))
        self.assertFalse(p.requests)
        p.observe(event("POST"))
        self.assertEqual(p.requests["1"][1], {"queryDomains": ["account"], "startTime": "2026-10-09"})


class Identities(unittest.IsolatedAsyncioTestCase):
    async def test_subaccount_allowed_but_other_shop_rejected(self):
        p = SimpleNamespace(
            session=SimpleNamespace(shop={"detected_name": "店铺甲"}),
            wait=AsyncMock(
                return_value=(
                    {"info": {"ok": True}, "data": {"meta": {"nickName": "店铺甲:运营", "memberId": 123}}},
                    {},
                )
            ),
        )
        self.assertEqual(await identity(p), "123")
        p.session.shop["detected_name"] = "店铺乙"
        with self.assertRaises(ValueError):
            await identity(p)
