import json
import unittest
from datetime import date
from unittest.mock import AsyncMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.reconciliation import options_for, normalize, summarize, collect, normalize_details, read_breakdown
from backend.business import create_router

DAY = "reconciliation_day"
MONTH = "reconciliation_month"


def bill(period="2024-02-29"):
    return dict(
        time=period,
        incomeAmount=101,
        outcomeAmount=-202,
        beforeAmount=303,
        afterAmount=202,
        incomeNumber=2,
        outcomeNumber=3,
    )


class ReconciliationTests(unittest.TestCase):
    def test_calendar_bounds_and_current_partial_year(self):
        today = date(2026, 10, 8)
        self.assertEqual(options_for(DAY, "2024-02", today)["date_to"], "2024-03-01")
        self.assertEqual(options_for(DAY, "2025-12", today)["date_to"], "2026-01-01")
        self.assertEqual(options_for(MONTH, "2025", today)["date_to"], "2026-01-01")
        self.assertEqual(options_for(MONTH, "", today)["date_to"], "2026-11-01")
        self.assertEqual(options_for(DAY, "", today)["period"], "2026-10")
        for kind, period in [(DAY, "2026-13"), (DAY, "2026-11"), (MONTH, "2027"), (MONTH, "1999"), (MONTH, "2026-10")]:
            with self.subTest(period=period), self.assertRaises(ValueError):
                options_for(kind, period, today)

    def test_signed_cents_and_missing_values(self):
        row = normalize(bill(), DAY)
        self.assertEqual(row["net"], -1.01)
        self.assertEqual(row["closing_balance"], 2.02)
        self.assertEqual(summarize([row] * 100)["net"], -101)
        self.assertIsNone(summarize([])["income"])
        for value in (None, True, "101", 1.01):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize({**bill(), "incomeAmount": value}, DAY)

    def test_combined_totals_cover_all_pages(self):
        rows = [
            {**normalize(bill(f"2024-02-{day:02}"), DAY), "shop_id": shop} for shop in (1, 2) for day in range(1, 25)
        ]

        class Service:
            def data(self, kind, options, shop_id, refresh, force):
                return {"rows": [r for r in rows if not shop_id or r["shop_id"] == shop_id]}

        app = FastAPI()
        app.include_router(create_router(Service(), lambda: None))
        with TestClient(app) as client:
            first = client.get("/api/data/reconciliation_day?period=2024-02&page_size=20").json()
            last = client.get("/api/data/reconciliation_day?period=2024-02&page_size=20&page=3").json()
            single = client.get("/api/data/reconciliation_day?period=2024-02&shop_id=2").json()
            self.assertEqual((first["total"], len(last["rows"])), (48, 8))
            self.assertEqual(first["finance_summary"], last["finance_summary"])
            self.assertEqual(first["finance_summary"]["income"], 48.48)
            self.assertEqual(single["finance_summary"]["income"], 24.24)
            self.assertEqual(client.get("/api/data/reconciliation_month?period=9999").status_code, 400)


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_monthly_partial_cutoff_and_source_request(self):
        page = type(
            "Page",
            (),
            {
                "prepare_reconciliation": AsyncMock(return_value={}),
                "evaluate": AsyncMock(
                    return_value=json.dumps(
                        {
                            "success": True,
                            "result": {"summaryList": [bill("2026-10")], "latestSummaryDay": "2026-10-07"},
                        }
                    )
                ),
            },
        )()
        result = await collect(page, MONTH, options_for(MONTH, "2026", date(2026, 10, 8)), include_details=False)
        self.assertEqual(result["rows"][0]["summary_through"], "2026-10-07")
        self.assertIn('"endMonth": "2026-11"', page.evaluate.call_args.args[0])

    async def test_reject_duplicate_out_of_range_and_incomplete_payloads(self):
        for result in ([bill(), bill()], [bill("2024-03-01")], None, {}):
            page = type(
                "Page",
                (),
                {
                    "prepare_reconciliation": AsyncMock(return_value={}),
                    "evaluate": AsyncMock(return_value=json.dumps({"success": True, "result": result})),
                },
            )()
            with self.subTest(result=result), self.assertRaises(ValueError):
                await collect(page, DAY, options_for(DAY, "2024-02"))

    async def test_daily_detail_uses_next_day_and_preserves_category_counts(self):
        raw = {
            "income": [{"financeId": 1, "description": "交易收入", "amount": 101, "number": 2}],
            "outcome": [{"financeId": 6, "description": "退款", "amount": -202, "number": 3}],
        }
        page = type(
            "Page",
            (),
            {"finance_headers": {}, "evaluate": AsyncMock(return_value=json.dumps({"success": True, "result": raw}))},
        )()
        row = normalize(bill(), DAY)
        result, through = await read_breakdown(page, row, DAY, None)
        self.assertEqual(through, "2024-02-29")
        self.assertIn('"endDate": "2024-03-01"', page.evaluate.call_args.args[0])
        details = normalize_details(result, row)
        self.assertEqual(details["income"][0]["count"], 2)
        self.assertEqual(details["outcome"][0]["amount"], -2.02)
        for bad in (
            {**raw, "income": []},
            {**raw, "outcome": None},
            {**raw, "income": [dict(raw["income"][0], number=1)]},
            {**raw, "income": [raw["income"][0], raw["income"][0]]},
        ):
            with self.assertRaises(ValueError):
                normalize_details(bad, row)

    async def test_month_detail_clamps_to_publication_cutoff(self):
        page = type(
            "Page",
            (),
            {"finance_headers": {}, "evaluate": AsyncMock(return_value=json.dumps({"success": True, "result": {}}))},
        )()
        row = normalize(bill("2026-10"), MONTH)
        _, through = await read_breakdown(page, row, MONTH, "2026-10-07")
        self.assertEqual(through, "2026-10-07")
        self.assertIn('"endDate": "2026-10-08"', page.evaluate.call_args.args[0])
