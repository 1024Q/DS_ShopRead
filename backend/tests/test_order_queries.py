import json, tempfile, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend import orders, collection_plan, table_index
from backend.business import options_for
from backend.operations import collect_operations
from backend.store import Store


class OrderQueries(unittest.TestCase):
    def test_native_tabs_and_warning_contract(self):
        opts = options_for("orders")
        for status, code, after in [
            ("", 0, 0),
            ("待付款", 7, 0),
            ("待发货", 1, 1),
            ("待收货", 2, 1),
            ("即将逾期发货", 1, 1),
        ]:
            body = orders.request_body({**opts, "status": status}, opts["end"] - 100)
            self.assertEqual((body["orderType"], body["afterSaleType"]), (code, after))
            self.assertEqual(body.get("delayShippingStatus"), 1 if status == "即将逾期发货" else None)
            self.assertEqual(body["groupEndTime"], opts["end"] - 100)
        with self.assertRaises(ValueError):
            options_for("orders", status="平台状态1")
        with self.assertRaises(ValueError):
            options_for("orders", date_from="2020-01-01")

    def test_status_scopes_do_not_share_cache_keys(self):
        for kind, status in [
            ("orders", "待发货"),
            ("orders", "即将逾期发货"),
            ("tickets", "待处理"),
            ("aftersales", "待处理即将逾期"),
        ]:
            opts = options_for(kind, status=status)
            self.assertEqual(collection_plan.canonical(kind, opts)["status"], status)

    def test_normalized_rows_sort_and_filter_before_pagination(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("fixture", "pw", name="测试店铺")
            shop = store.list()[0]
            rows = [
                orders.normalize(
                    {
                        "order_sn": str(i),
                        "goods_id": 456,
                        "goods_name": "商品",
                        "order_status_str": "已发货",
                        "goods_number": 2,
                        "order_amount": 100 * i,
                        "created_at": 1791700000,
                        "receive_name": "不保存的个人信息",
                    },
                    "待收货",
                )
                for i in range(1, 26)
            ]
            self.assertNotIn("receive_name", rows[0])
            self.assertEqual(rows[0]["amount"], 1)
            store.write_cache(sid, "orders:fixture", {"rows": rows, "complete": True})
            result = table_index.query(
                store,
                [(shop, "orders:fixture")],
                "orders",
                status="待收货",
                sort_by="amount",
                sort_direction="desc",
                page_size=20,
            )
            self.assertEqual(result["total"], 25)
            self.assertEqual(result["rows"][0]["id"], "25")
            self.assertEqual(len(result["rows"]), 20)

    def test_shipping_warning_rejects_shipped_cached_rows(self):
        from backend.business import filter_rows

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("fixture", "pw")
            shop = store.list()[0]
            rows = [
                {"id": str(i), "status": status, "attention_status": "即将逾期发货"}
                for i, status in enumerate(["已发货，待收货", "已取消", "未知", "已成团，待发货"])
            ]
            store.write_cache(sid, "orders:fixture", {"rows": rows, "complete": True})
            self.assertEqual([r["id"] for r in filter_rows(rows, "orders", status="即将逾期发货")], ["3"])
            result = table_index.query(store, [(shop, "orders:fixture")], "orders", status="即将逾期发货", page_size=1)
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["rows"][0]["id"], "3")

    def test_normalization_never_tags_shipped_as_shipping_warning(self):
        row = orders.normalize({"order_sn": "x", "order_status_str": "已发货，待收货"}, "即将逾期发货")
        self.assertEqual(row["attention_status"], "")


class WarningQueries(unittest.IsolatedAsyncioTestCase):
    async def test_warning_uses_native_filter_without_default_week_window(self):
        part = {"rows": [], "source_total": 0, "source_loaded": 0, "complete": True}
        with patch("backend.operations.paginate", new=AsyncMock(return_value=part)) as read:
            await collect_operations(
                SimpleNamespace(), "aftersales", options_for("aftersales", status="待处理即将逾期")
            )
            body = read.call_args.args[2]
            self.assertEqual(body["quickSearchType"], 4)
            self.assertNotIn("startCreatedTime", body)

    async def test_malformed_order_response_is_not_successful_empty_snapshot(self):
        page = type("Page", (), {"search": AsyncMock(return_value={"success": True, "result": {}})})()
        with self.assertRaises(ValueError):
            await orders.collect(page, options_for("orders", status="待发货"))


class SegmentedOrders(unittest.IsolatedAsyncioTestCase):
    async def test_selective_warning_does_not_scan_ninety_empty_days(self):
        with patch("backend.orders.collect_single", new=AsyncMock(return_value={"rows": [], "complete": True})) as read:
            await orders.collect(object(), options_for("orders", status="即将逾期发货"))
        self.assertEqual(read.await_count, 1)

    async def test_full_range_is_covered_without_overlap(self):
        calls = []

        async def read(page, options):
            calls.append((options["start"], options["end"]))
            return {"rows": [{"id": str(options["start"])}], "complete": True}

        with patch("backend.orders.collect_single", side_effect=read):
            result = await orders.collect(object(), {"start": 100, "end": 100 + 20 * 86400, "status": ""})
        self.assertEqual(len(calls), 21)
        self.assertEqual(calls[0][0], 100)
        self.assertEqual(calls[-1][1], 100 + 20 * 86400)
        self.assertTrue(all(a[1] + 1 == b[0] for a, b in zip(calls, calls[1:])))
        self.assertTrue(result["complete"])

    async def test_range_cap_splits_and_deduplicates(self):
        async def read(page, options):
            if options["end"] - options["start"] > 4000:
                raise ValueError("请缩小查询范围")
            return {"rows": [{"id": "same"}], "complete": True}

        with patch("backend.orders.collect_single", side_effect=read):
            result = await orders.collect(object(), {"start": 100, "end": 8100, "status": ""})
        self.assertEqual(len(result["rows"]), 1)

    async def test_permission_is_not_retried_by_range_splitting(self):
        with patch("backend.orders.collect_single", side_effect=ValueError("权限不足")) as read:
            with self.assertRaisesRegex(ValueError, "权限不足"):
                await orders.collect(object(), {"start": 100, "end": 8100, "status": ""})
        self.assertEqual(read.call_count, 1)

    async def test_small_range_rejection_requires_action(self):
        from backend.collection_errors import classify, guidance

        with patch("backend.orders.collect_single", side_effect=ValueError("订单查询异常")):
            try:
                await orders.collect(object(), {"start": 100, "end": 800, "status": ""})
            except ValueError as error:
                category, blocked = classify(error)
                self.assertEqual(category, "range_limit")
                self.assertTrue(blocked)
                self.assertFalse(guidance("orders", str(error), category, blocked)["automatic"])
            else:
                self.fail("must not publish empty success")


if __name__ == "__main__":
    unittest.main()
