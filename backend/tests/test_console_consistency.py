import tempfile, unittest
from unittest.mock import AsyncMock, patch
from backend.store import Store
from backend import orders, table_index, new_products
from backend.business import filter_rows, options_for
from backend.read_errors import PageReadError


class ConsoleDataTests(unittest.TestCase):
    def test_operator_filter_and_sort_cover_all_pages(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            for name, people in [("A", ["Zoe", "Amy"]), ("B", ["Bob"])]:
                sid = store.add(name, "test", name=name, operator=people)
                store.write_cache(
                    sid,
                    "products:test",
                    {
                        "rows": [{"id": str(n), "name": "商品", "created_at": "2026-10-11"} for n in range(25)],
                        "complete": True,
                    },
                )
            shops = store.list()
            selection = [(s, "products:test") for s in shops]
            result = table_index.query(store, selection, "products", keyword="amy", page_size=10)
            self.assertEqual(result["total"], 25)
            self.assertEqual(len(result["rows"]), 10)
            self.assertEqual(
                table_index.query(store, selection, "products", sort_by="operators", sort_direction="asc")["rows"][0][
                    "shop_id"
                ],
                shops[0]["id"],
            )
            self.assertEqual(len(filter_rows([{"operators": ["Amy"], "name": "x"}], "products", keyword="amy")), 1)

    def test_calendar_aggregates_creation_days_without_fetching_catalog(self):
        data = {
            "rows": [],
            "sources": [
                {
                    "shop_id": 1,
                    "updated_at": "now",
                    "operators": ["Amy", "Bob"],
                    "complete": True,
                    "_creation_counts": {"unknown": 0, "days": {"2026-10-01": 3, "2026-10-11": 2, "2026-09-01": 9}},
                }
            ],
        }
        result = new_products.summarize(data, "2026-10-11", 2025)
        for person in result["operators"]:
            self.assertEqual(person["days"], {"2026-10-01": 3, "2026-10-11": 2})
            self.assertEqual(person["count"], 2)

    def test_refund_contract_and_partial_refund_amount(self):
        body = orders.request_body(options_for("orders", status="退款成功"))
        self.assertEqual(body["afterSaleType"], 4)
        metrics = orders.refund_metrics(
            [{"amount": 100, "refund_amount": 12.34}, {"amount": 80, "refund_amount": 0.01}], True, "2026-10-11"
        )
        self.assertEqual(metrics["refund_orders"], 2)
        self.assertEqual(metrics["refund_amount"], 12.35)
        self.assertIsNone(orders.refund_metrics([{"amount": 100}], True, "2026-10-11")["refund_amount"])
        self.assertEqual(orders.refund_metrics([], True, "2026-10-11")["refund_amount"], 0)


class RefundJoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_join_discards_frozen_checkpoint(self):
        from backend.collection_checkpoint import current
        from unittest.mock import Mock

        ctx = type("Checkpoint", (), {"clear": Mock(), "store": Mock(), "shop": 1})()
        token = current.set(ctx)
        try:
            page = type("Page", (), {"open": AsyncMock()})()
            result = {"rows": [{"order_number": "o1", "aftersale_id": "case1"}], "complete": True}
            with (
                patch("backend.operations.paginate", new=AsyncMock(return_value={"rows": []})),
                patch("backend.refund_index.lookup", return_value={}),
            ):
                with self.assertRaises(PageReadError):
                    await orders.with_refunds(page, result, {"start": 1, "date_from": "2026-10-11"}, 2)
            ctx.clear.assert_called_once()
        finally:
            current.reset(token)

    async def test_exact_case_id_and_order_must_match(self):
        page = type("Page", (), {"open": AsyncMock()})()
        result = {
            "rows": [{"id": "o1", "order_number": "o1", "aftersale_id": "case1", "amount": 100}],
            "complete": True,
        }
        options = {"start": 1, "date_from": "2026-10-11"}
        with patch(
            "backend.operations.paginate",
            new=AsyncMock(return_value={"rows": [{"id": "old", "order_number": "o1", "amount": 100}]}),
        ):
            with self.assertRaises(PageReadError):
                await orders.with_refunds(page, result, options, 2)
        with patch(
            "backend.operations.paginate",
            new=AsyncMock(return_value={"rows": [{"id": "case1", "order_number": "o1", "amount": 12.34}]}),
        ):
            data = await orders.with_refunds(page, result, options, 2)
            self.assertEqual(data["metrics"]["refund_amount"], 12.34)


if __name__ == "__main__":
    unittest.main()
