import unittest
from unittest.mock import patch, AsyncMock
from backend.xiaohongshu_tax import normalize_bill, collect
from backend.tax_info import options_for
from backend.platform_support import supports, source_page


class XhsTaxTests(unittest.TestCase):
    def fixture(self):
        bill = {
            "processStatus": "SUCCESS",
            "currency": "CNY",
            "startTime": "2026-09-01 00:00:00",
            "endTime": "2026-10-01 00:00:00",
            "companyName": "测试主体",
            "statementNo": "S1",
            "settleOrderCount": 2,
            "totalChangeAmount": "85",
            "totalCommissionAmount": "5",
            "ext": {"uniformCode": "CODE"},
            "feeDetailInfo": [
                {"code": "STATEMENT_IN", "displayValue": "100"},
                {"code": "STATEMENT_REFUND", "displayValue": "-10"},
            ],
        }
        sheets = {
            "总览": [{"A": "公司名称：", "B": "测试主体"}, {"A": "结算期间：", "B": "2026-09-01~2026-09-30"}],
            "订单结算": [
                {"A": "订单号", "B": "计佣基数", "C": "动账金额", "D": "结算时间"},
                {"A": "O1", "B": "105", "C": "100", "D": "2026-09-30 00:00:00"},
                {"A": "O1", "B": "-10", "C": "-10", "D": "2026-09-30 00:00:00"},
            ],
        }
        return bill, sheets

    def test_signed_bases_not_settlement_net_or_deduplicated_order_ids(self):
        bill, sheets = self.fixture()
        with patch("backend.xiaohongshu_tax.workbook_rows", return_value=sheets):
            row = normalize_bill(bill, b"")
        self.assertEqual(row["tax_amount_cents"], 9500)
        self.assertEqual(row["settlement_net_cents"], 8500)
        self.assertEqual(row["goods_refund_cents"], 1000)
        self.assertEqual(row["entity"]["credit_code"], "CODE")
        self.assertNotIn("service_net", row)
        self.assertTrue(supports("xiaohongshu", "tax"))
        self.assertIn("/settle/monthly", source_page("xiaohongshu", "tax"))

    def test_incomplete_mismatched_month_subject_or_amount_rejected(self):
        for change in [
            {"settleOrderCount": 3},
            {"companyName": "其他主体"},
            {"startTime": "2026-08-01 00:00:00"},
            {
                "feeDetailInfo": [
                    {"code": "STATEMENT_IN", "displayValue": "101"},
                    {"code": "STATEMENT_REFUND", "displayValue": "-10"},
                ]
            },
        ]:
            bill, sheets = self.fixture()
            with self.subTest(change=change), patch("backend.xiaohongshu_tax.workbook_rows", return_value=sheets):
                with self.assertRaises(ValueError):
                    normalize_bill({**bill, **change}, b"")


class XhsQuarterTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_quarter_month_fails_before_download(self):
        page = AsyncMock()
        page.evaluate.return_value = True
        page.request.return_value = {"list": [{"startTime": "2026-09-01"}], "total": 1}
        with patch("backend.xiaohongshu_tax.XhsPage") as factory:
            factory.return_value.__aenter__.return_value = page
            with self.assertRaisesRegex(ValueError, "三个月"):
                await collect(None, options_for("2026-Q3"))
        payload = page.request.call_args.args[1]
        self.assertEqual(payload["startTime"], "2026-07-01 00:00:00")
        self.assertEqual(payload["endTime"], "2026-10-01 00:00:00")
