import unittest
from backend.platform_tax import statement, tmall_rows, jd_rows
from backend.tax_info import group_rows, summarize
from backend.xlsx_export import response
from backend.platform_support import supports, source_page
from zipfile import ZipFile
from io import BytesIO


class PlatformTaxTests(unittest.TestCase):
    def test_tmall_quarter_is_not_a_month_and_retains_official_fields(self):
        item = {
            "reportPeriod": "2026年第3季度 07-09月",
            "goodsIncome": {"totalAmount": 100, "refundAmount": 3, "netAmount": 97},
            "serviceIncome": {"totalAmount": 5, "refundAmount": 1, "netAmount": 4},
            "platformCommissionTotal": 8,
        }
        rows, entity = tmall_rows(
            {"billItems": [item], "subjectName": "测试公司", "authenticateType": "ENTERPRISE"}, "2026-Q3"
        )
        row = rows[0]
        self.assertEqual(row["tax_amount_cents"], 10100)
        self.assertEqual(row["summary_through"], "2026-09-30")
        self.assertNotIn("coupon_subsidy", row)
        self.assertEqual(tmall_rows({"billItems": [item], "subjectName": "测试公司"}, "2026-Q2")[0], [])
        item["goodsIncome"]["netAmount"] = 96
        with self.assertRaises(ValueError):
            tmall_rows({"billItems": [item], "subjectName": "测试公司"}, "2026-Q3")

    def test_jd_does_not_double_count_months_and_quarters_or_lose_subjects(self):
        item = {
            "year": 2026,
            "quarter": 3,
            "month": None,
            "companyName": "甲公司",
            "goodIncomeTotal": 100,
            "goodRefund": 3,
            "goodNet": 97,
            "serviceIncomeTotal": 5,
            "serviceRefund": 1,
            "serviceNet": 4,
            "settleCommissionNotAdv": 2,
            "advNotCps": 3,
            "advCps": 4,
            "orderNum": 8,
        }
        monthly = {**item, "quarter": None, "month": 9}
        second = {**item, "companyName": "乙公司"}
        data = {"taxFilings": [item, monthly, second], "companyInfo": {"1": "甲公司", "2": "乙公司"}}
        rows, entities = jd_rows(data, "2026-Q3")
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(entities), 2)
        self.assertEqual(rows[0]["platform_commission_cents"], 900)
        self.assertEqual(len(jd_rows(data, "2026-09")[0]), 1)
        with self.assertRaises(ValueError):
            jd_rows({**data, "total": 4}, "2026-Q3")

    def test_group_summary_and_excel_keep_platform_basis_separate(self):
        amounts = {
            "goods_income": 100,
            "goods_refund": 3,
            "goods_net": 97,
            "service_income": 5,
            "service_refund": 1,
            "service_net": 4,
            "platform_commission": 8,
        }
        base = statement("2026-Q3", {"company_name": "同名主体"}, amounts)
        rows = group_rows(
            [{**base, "platform": p, "shop_id": i, "shop_name": str(i)} for i, p in [(1, "taobao"), (2, "jd")]]
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(summarize(rows)["official_net"], 202)
        self.assertIsNone(summarize(rows)["trade_income"])
        with ZipFile(BytesIO(response("tax", {"rows": rows}, {}).body)) as z:
            xml = z.read("xl/worksheets/sheet1.xml").decode()
            self.assertIn("2026-Q3", xml)
            self.assertIn("货物收入总额", xml)
            self.assertIn("报税数据口径", xml)
        for platform in ("taobao", "jd"):
            self.assertTrue(supports(platform, "tax"))
            self.assertIn("http", source_page(platform, "tax"))
