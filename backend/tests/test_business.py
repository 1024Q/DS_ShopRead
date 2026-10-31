import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock
from backend.store import Store
from backend.browser import Session
from backend.business_reader import normalize, parse_metrics, fetch_list
from backend.business import options_for, filter_rows, BusinessService


class DataTests(unittest.TestCase):
    def test_metrics_distinguish_missing_and_zero(self):
        result = parse_metrics(
            "成交金额\n1,234.56\n成交订单数\n10\n待处理工单\n0\n实时数据更新时间: 2026-10-08 16:00:00"
        )
        self.assertEqual(result["sales"], 1234.56)
        self.assertEqual(result["pending_tickets"], 0)
        self.assertIsNone(result["visitors"])
        with self.assertRaises(ValueError):
            parse_metrics("请登录")

    def test_product_currency_and_zero_stock(self):
        row = normalize("products", {"id": 123, "sku_group_price": [1000, 1500], "quantity": 0, "is_onsale": True})
        self.assertEqual((row["price_min"], row["price_max"], row["status"]), (10, 15, "已售罄"))

    def test_review_id_and_correct_score(self):
        row = normalize(
            "reviews",
            {
                "reviewId": 778850300749571016,
                "score": 0,
                "descScore": 2,
                "comment": "内容",
                "reply": "",
                "replyStatus": 0,
            },
        )
        self.assertEqual(row["id"], "778850300749571016")
        self.assertEqual(row["stars"], 2)
        self.assertFalse(row["replied"])

    def test_filters_combine_instead_of_matching_any(self):
        rows = [
            {"id": "a", "stars": 2, "content": "气味", "replied": False, "created_at": "2026-10-08 12:00:00"},
            {"id": "b", "stars": 5, "content": "气味", "replied": False, "created_at": "2026-10-08 12:00:00"},
            {"id": "c", "stars": 2, "content": "气味", "replied": True, "created_at": "2026-10-08 12:00:00"},
        ]
        result = filter_rows(rows, "reviews", "气味", stars=[1, 2, 3], reply="unreplied", date_from="2026-10-08")
        self.assertEqual([r["id"] for r in result], ["a"])

    def test_dates_are_beijing_and_invalid_ranges_rejected(self):
        options = options_for("reviews", "2026-10-08", "2026-10-08", stars="3,1,1")
        self.assertEqual(options["end"] - options["start"], 86399)
        self.assertEqual(options["stars"], [1, 3])
        self.assertEqual(options["start"] % 86400, 16 * 3600)
        with self.assertRaises(ValueError):
            options_for("reviews", "2026-10-09", "2026-10-08")
        with self.assertRaises(ValueError):
            options_for("reviews", stars="6")


class AsyncDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_reviews_use_filtered_count_and_keep_large_ids(self):
        payload = {
            "success": True,
            "result": {"totalNum": 364, "totalRows": 1, "data": [{"reviewId": 778850300749571016, "descScore": 1}]},
        }

        class Fake:
            evaluate = AsyncMock(return_value=json.dumps(payload))

        result = await fetch_list(Fake(), "reviews", options_for("reviews", stars="1"))
        self.assertEqual(result["source_total"], 1)
        self.assertTrue(result["complete"])
        self.assertEqual(result["rows"][0]["id"], "778850300749571016")
        Fake.evaluate.assert_awaited_once()

    async def test_deleted_or_changed_shop_cache_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            shop_id = store.add("one", "pw")
            store.write_cache(shop_id, "reviews:test", {"rows": [{"id": "1"}]})
            store.update(shop_id, "two", "pw")
            self.assertIsNone(store.read_cache(shop_id, "reviews:test"))
            store.write_cache(shop_id, "overview:test", {"metrics": {"orders": 1}})
            store.delete(shop_id)
            self.assertIsNone(store.read_cache(shop_id, "overview:test"))

    async def test_all_shops_and_missing_shop_data_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.add("one", "pw")
            store.add("two", "pw")
            sessions = {r["id"]: Session(r, store) for r in store.list()}
            service = BusinessService(store, lambda shop_id: sessions[shop_id])
            options = options_for("overview")
            record = store.list()[0]
            store.write_cache(
                record["id"],
                service.key("overview", options, record),
                {"metrics": {"orders": 0, "sales": 0}, "rows": [], "complete": True},
            )
            result = service.data("overview", options)
            self.assertEqual(len(result["sources"]), 2)
            self.assertEqual(len(result["metrics"]), 1)
            self.assertEqual(result["totals"]["orders"], 0)
            self.assertIsNone(result["totals"]["visitors"])
