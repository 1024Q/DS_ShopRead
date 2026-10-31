import copy
import unittest
from datetime import date
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.tax_info import (
    options_for,
    normalize_entity,
    normalize_breakdown,
    summarize,
    group_rows,
    subject_key,
    subject_options,
)
from backend.business import create_router


def detail():
    return {
        "income": [
            {"financeId": 1, "description": "交易收入", "amount": 10001},
            {"financeId": 2, "description": "优惠券补贴", "amount": 202},
            {"financeId": 5, "description": "其他收入", "amount": 900},
        ],
        "outcome": [
            {"financeId": 6, "description": "退款", "amount": -303},
            {"financeId": 7, "description": "退款退优惠券", "amount": -40},
            {"financeId": 9, "description": "提现", "amount": -5000},
        ],
    }


MONTH = {"period": "2026-09", "created_at": "2026-09-01", "income_cents": 11103, "outcome_cents": -5343}
ENTITY = {"company_name": "示例企业"}


class TaxTests(unittest.TestCase):
    def test_current_year_and_future_rejected(self):
        self.assertEqual(options_for(today=date(2026, 1, 1))["period"], "2026-01")
        self.assertEqual(options_for("2024-02", date(2026, 10, 8))["date_to"], "2024-03-01")
        with self.assertRaises(ValueError):
            options_for("2026-11", date(2026, 10, 8))

    def test_four_terms_use_signed_refunds_and_exclude_other_income_and_withdrawals(self):
        row = normalize_breakdown(detail(), MONTH, ENTITY, "2026-09-30")
        self.assertEqual(row["refund"], 3.03)
        self.assertEqual(row["coupon_refund"], 0.4)
        self.assertEqual(row["tax_amount_cents"], 9860)
        self.assertEqual(row["tax_amount"], 98.6)
        self.assertFalse(row["partial"])
        self.assertEqual(summarize([row] * 100)["tax_amount"], 9860)
        self.assertIsNone(summarize([])["tax_amount"])

    def test_absent_zero_categories_only_after_total_verification(self):
        empty = {"income": [], "outcome": []}
        row = normalize_breakdown(empty, {**MONTH, "income_cents": 0, "outcome_cents": 0}, ENTITY, "2026-09-12")
        self.assertEqual(row["tax_amount"], 0)
        self.assertTrue(row["partial"])
        with self.assertRaises(ValueError):
            normalize_breakdown(empty, MONTH, ENTITY, "2026-09-30")
        with self.assertRaises(ValueError):
            normalize_breakdown({"income": []}, MONTH, ENTITY, "2026-09-30")

    def test_schema_sign_duplicates_and_category_changes_rejected(self):
        cases = []
        for field, value in [("amount", 303), ("amount", None), ("financeId", 60), ("description", "其他退款")]:
            data = detail()
            data["outcome"][0][field] = value
            cases.append(data)
        data = detail()
        data["income"].append(copy.deepcopy(data["income"][0]))
        cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                normalize_breakdown(data, MONTH, ENTITY, "2026-09-30")

    def test_entity_has_reporting_fields_and_no_identity_documents(self):
        entity = normalize_entity(
            {
                "queryDetailResult": {
                    "mallName": "示例店",
                    "mallId": 123,
                    "enterprise": {
                        "companyName": "示例企业",
                        "socialCreditUnicode": "TEST123",
                        "businessLicenceEndTime": "9999-09-09",
                        "legalRepresentativeIdcardNumber": "private",
                        "businessLicenceImgUrl": "private",
                        "mmsUserPasswd": "private",
                    },
                }
            },
            "工商企业",
        )
        self.assertEqual(entity["credit_code"], "TEST123")
        self.assertEqual(entity["license_end"], "长期")
        self.assertNotIn("private", str(entity))

    def test_combined_tax_total_survives_pagination(self):
        row = normalize_breakdown(detail(), MONTH, ENTITY, "2026-09-30")

        class Service:
            def data(self, kind, options, shop_id, refresh, force):
                return {
                    "rows": [
                        {**row, "shop_id": i, "shop_name": str(i), "entity": {"company_name": str(i)}}
                        for i in (1, 2)
                        if not shop_id or i == shop_id
                    ]
                }

        app = FastAPI()
        app.include_router(create_router(Service(), lambda: None))
        with TestClient(app) as client:
            first = client.get("/api/data/tax?period=2026-09&page_size=1").json()
            second = client.get("/api/data/tax?period=2026-09&page_size=1&page=2").json()
            single = client.get("/api/data/tax?period=2026-09&shop_id=1").json()
            self.assertEqual(first["tax_summary"]["tax_amount"], 197.2)
            self.assertEqual(first["tax_summary"], second["tax_summary"])
            self.assertEqual(single["tax_summary"]["tax_amount"], 98.6)
            self.assertEqual((first["total"], len(first["rows"])), (2, 1))

    def test_year_scope_and_subject_month_grouping(self):
        options = options_for("2025", date(2026, 10, 8))
        self.assertEqual((options["date_from"], options["date_to"]), ("2025-01-01", "2026-01-01"))
        self.assertEqual(options_for("2026", date(2026, 10, 8))["date_to"], "2026-11-01")
        with self.assertRaises(ValueError):
            options_for("2027", date(2026, 10, 8))
        row = normalize_breakdown(detail(), MONTH, ENTITY, "2026-09-30")
        first = {"company_name": "相同名称", "credit_code": "CODE1"}
        second = {"company_name": "相同名称", "credit_code": "CODE2"}
        rows = [
            {**row, "shop_id": 1, "shop_name": "甲店", "entity": first},
            {**row, "shop_id": 2, "shop_name": "乙店", "entity": first},
            {**row, "shop_id": 3, "shop_name": "丙店", "entity": second},
        ]
        merged = group_rows(rows)
        self.assertEqual(len(merged), 2)
        selected = group_rows(rows, subject_key(first))
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["tax_amount_cents"], 19720)
        self.assertEqual(len(selected[0]["shop_breakdown"]), 2)
        self.assertEqual(group_rows(rows, "unknown"), [])
        subjects = subject_options(rows)
        self.assertEqual(len(subjects), 2)
        self.assertEqual(sorted(len(s["shop_names"]) for s in subjects), [1, 2])

    def test_quarter_bounds_and_current_quarter_are_beijing_calendar_months(self):
        today = date(2026, 10, 8)
        for period, first, last in [
            ("2025-Q4", "2025-10-01", "2026-01-01"),
            ("2026-Q3", "2026-07-01", "2026-10-01"),
            ("2026-Q4", "2026-10-01", "2026-11-01"),
        ]:
            result = options_for(period, today)
            self.assertEqual((result["date_from"], result["date_to"]), (first, last))
        for period in ["2026-Q0", "2026-Q5", "2027-Q1", "1999-Q1"]:
            with self.assertRaises(ValueError):
                options_for(period, today)
        with self.assertRaises(ValueError):
            options_for("2026-Q4", date(2026, 8, 1))
