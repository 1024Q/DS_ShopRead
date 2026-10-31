from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from backend.operations import normalize_operation, collect_operations, paginate, decorate
from backend.business import options_for, filter_rows


class OperationTests(unittest.TestCase):
    def test_intercept_packages_are_not_lost_or_mislabelled(self):
        item = {
            "orderSn": "order-1",
            "shipList": [
                {"trackingNumber": "00123", "shippingId": 1, "interceptStatus": 2},
                {"trackingNumber": "00456", "shippingId": 1, "interceptStatus": 5},
            ],
        }
        rows = normalize_operation("intercepts", item)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["tracking_number"], "00123")
        self.assertEqual(rows[0]["priority"], 3)
        self.assertEqual(rows[1]["status"], "已撤销拦截")
        self.assertEqual(rows[1]["priority"], 0)

    def test_quality_rates_and_plain_suggestions(self):
        row = normalize_operation(
            "quality",
            {
                "goodsId": 1,
                "seriousInferiorData": {"ratio": 0.0006},
                "badReviewData": {"ratio": 0.0463},
                "rectificationMeasure": "<p>整改&amp;说明</p>",
            },
            "预警商品",
        )[0]
        self.assertEqual(row["negative_rate"], 0.06)
        self.assertEqual(row["bad_rate"], 4.63)
        self.assertEqual(row["suggestion"], "整改&说明")

    def test_pickup_deadline_is_shipping_plus_24_hours_beijing(self):
        from backend.package_timing import fields
        from datetime import datetime

        row = {"status": "发货未揽件", "created_at": "2026-10-11 12:30:00"}
        now = datetime.fromisoformat("2026-10-12T10:30:00+08:00").timestamp()
        self.assertEqual(
            fields(row, remaining=True, now=now),
            {"pickup_deadline": "2026-10-12T12:30:00+08:00", "pickup_hours_remaining": 2},
        )
        self.assertEqual(fields(row, remaining=True, now=now + 3 * 3600)["pickup_hours_remaining"], -1)
        for other in [
            {**row, "status": "已签收"},
            {**row, "platform": "taobao"},
            {**row, "created_at": ""},
            {**row, "created_at": "invalid"},
        ]:
            self.assertIsNone(fields(other, remaining=True)["pickup_hours_remaining"])

    def test_quality_priority_requires_effective_platform_status(self):
        for code, expected in [(1, 1), ("1", 1), (0, 0), (2, 0), (3, 0), (None, 0)]:
            row = normalize_operation(
                "quality",
                {"goodsId": 1, "status": code, "punishType": "商品降权", "startTime": "2026-01-01", "endTime": "--"},
                "品质违规商品",
            )[0]
            self.assertEqual(row["priority"], expected)
            self.assertEqual(decorate("quality", row, "taobao")["priority"], 0)
        for label in ("预警商品", "其他商品"):
            row = normalize_operation("quality", {"goodsId": 1, "status": 1}, label)[0]
            self.assertEqual(row["priority"], 0)
        self.assertEqual(
            decorate("quality", {"status": "品质违规商品", "priority": 1, "progress": "商品降权"})["priority"], 0
        )

    def test_evidence_ids_distinguish_multiple_requests_and_priorities(self):
        a = {
            "afterSalesId": "123",
            "orderSn": "order",
            "goodsId": 987654321098765432,
            "status": 0,
            "createTime": 1791400000000,
        }
        r1 = normalize_operation("evidence", a)[0]
        r2 = normalize_operation("evidence", {**a, "createTime": 1791400000001, "status": 3})[0]
        self.assertNotEqual(r1["id"], r2["id"])
        self.assertEqual(r1["priority"], 3)
        self.assertEqual(r2["priority"], 0)
        self.assertEqual(r1["goods_id"], "987654321098765432")

    def test_priority_and_tracking_filters_combine(self):
        rows = [
            decorate(
                "intercepts", {"id": "a", "status": "拦截失败", "carrier": "申通快递", "tracking_number": "00123"}
            ),
            decorate(
                "intercepts", {"id": "b", "status": "拦截成功", "carrier": "申通快递", "tracking_number": "00123"}
            ),
        ]
        result = filter_rows(rows, "intercepts", keyword="00123", priority_only=True, carrier="申通")
        self.assertEqual([r["id"] for r in result], ["a"])
        self.assertEqual(filter_rows(rows, "intercepts", keyword="00123", priority_only=True, carrier="中通"), [])

    def test_ticket_and_platform_status_priority(self):
        self.assertEqual(decorate("tickets", {"status": "违规已处理"})["priority"], 0)
        self.assertEqual(decorate("aftersales", {"status": "平台介入待商家处理"})["priority"], 3)
        self.assertEqual(decorate("aftersales", {"status": "平台处理中"})["priority"], 3)

    def test_brand_absence_uses_actual_list_not_an_inferred_flag(self):
        row = normalize_operation(
            "brands", {"goodsId": 123, "loseBlackTag": False, "nonBlackLabelReasons": ["商品领航员不满足要求"]}
        )[0]
        self.assertEqual(row["status"], "无黑标商品")
        self.assertEqual(row["problem"], "商品领航员不满足要求")


class AsyncOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigator_carries_platform_total_into_next_page(self):
        calls = []

        async def fake(session, path, body):
            calls.append(dict(body))
            return {"total": 2, "goodsList": [{"goodsId": body["pageNo"]}]}

        with patch("backend.operations.read", fake):
            result = await paginate(None, "navigator", {"pageNo": 1, "total": 0}, "pageNo", "goodsList")
        self.assertEqual(calls[1]["total"], 2)
        self.assertTrue(result["complete"])

    async def test_changing_duplicate_page_is_not_counted_as_complete(self):
        fake = AsyncMock(
            side_effect=[
                {"total": 2, "goodsList": [{"goodsId": 1, "quantity": 3}]},
                {"total": 2, "goodsList": [{"goodsId": 1, "quantity": 2}]},
            ]
        )
        with patch("backend.operations.read", fake):
            result = await paginate(None, "navigator", {"pageNo": 1}, "pageNo", "goodsList")
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["rows"]), 1)

    async def test_null_total_is_read_until_has_more_false(self):
        fake = AsyncMock(
            side_effect=[
                {"total": None, "hasMore": True, "highPriceGoodsList": [{"goodsId": 1}]},
                {"total": None, "hasMore": False, "highPriceGoodsList": [{"goodsId": 2}]},
            ]
        )
        with patch("backend.operations.read", fake):
            result = await paginate(None, "brands", {"pageNum": 1}, "pageNum", "highPriceGoodsList")
        self.assertTrue(result["complete"])
        self.assertEqual(result["source_total"], 2)

    async def test_priority_query_survives_default_date_window_and_deduplicates(self):
        captured = []

        async def fake(session, kind, body, *args):
            captured.append(dict(body))
            label = args[-1]
            row = decorate(kind, {"id": "1", "status": label or "退款，退款成功"})
            return {"rows": [row], "source_total": 1, "source_loaded": 1, "complete": True}

        with patch("backend.operations.paginate", fake):
            result = await collect_operations(SimpleNamespace(), "aftersales", options_for("aftersales"))
        self.assertNotIn("startCreatedTime", captured[0])
        self.assertIn("startCreatedTime", captured[-1])
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["status"], "平台介入待商家处理")

    async def test_quality_violations_separate_collection_not_zero_total(self):
        with patch(
            "backend.operations.read",
            AsyncMock(
                return_value={
                    "total": 0,
                    "goodsInfoList": [],
                    "goodsPunishRecordVOList": [{"goodsId": 1, "startTime": "2026-10-08"}],
                }
            ),
        ):
            result = await collect_operations(
                SimpleNamespace(), "quality", options_for("quality", status="品质违规商品")
            )
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["source_total"], 1)

    async def test_permission_denial_is_not_converted_to_empty_success(self):
        from backend.operations import read

        class Fake:
            evaluate = AsyncMock(return_value='{"success":true,"result":{"hasPermission":false}}')

        with self.assertRaisesRegex(ValueError, "权限"):
            await read(Fake(), "/link/api/blacktag/goods/compare/price/goods/list", {})
