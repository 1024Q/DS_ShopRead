import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from backend.store import Store
from backend import table_index, tax_review
from backend.table_sort import sort_rows
from backend.tax_info import options_for, group_rows
from backend.tests.test_audit_store import row


class RequestedFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("fixture", "password")

    def test_sales_30d_numeric_sort_pagination_export_and_nulls(self):
        rows = [{"id": str(i), "sales_30d": i, "status": "在售中"} for i in range(45)] + [
            {"id": "missing", "sales_30d": None}
        ]
        key = "products:test"
        self.store.write_cache(self.sid, key, {"rows": rows, "complete": True})
        selections = [(self.store.list()[0], key)]
        for direction in ("asc", "desc"):
            expected = sort_rows(rows, "sales_30d", direction)
            result = table_index.query(
                self.store, selections, "products", sort_by="sales_30d", sort_direction=direction, page=2, page_size=20
            )
            self.assertEqual([r["sales_30d"] for r in result["rows"]], [r["sales_30d"] for r in expected[20:40]])
            exported = table_index.query(
                self.store, selections, "products", sort_by="sales_30d", sort_direction=direction, export=True
            )
            self.assertEqual([r["sales_30d"] for r in exported["rows"]], [r["sales_30d"] for r in expected])
        for field in ("sales_30d');DROP TABLE shops;--", "a.b", "1sales"):
            with self.assertRaises(ValueError):
                sort_rows(rows, field)
            with self.assertRaises(ValueError):
                table_index.query(self.store, selections, "products", sort_by=field)

    def test_missing_month_keeps_latest_amount_and_history(self):
        opts = options_for("2026-09")
        for amount in (100, 200):
            self.store.compare_tax(self.sid, {"rows": [row(amount=amount)]}, opts)
        missing = self.store.compare_tax(self.sid, {"rows": []}, opts)["rows"][0]
        self.assertEqual(missing["tax_amount_cents"], 200)
        self.assertTrue(missing["db_comparison"]["missing"])
        self.assertFalse(missing["db_comparison"]["attention"])
        self.assertEqual(self.store.tax_snapshot(self.sid, opts)[0]["tax_amount_cents"], 200)
        history = tax_review.read(self.store, self.sid, "2026-09")
        self.assertEqual(history["history_total"], 1)
        self.assertEqual(history["baseline"]["tax_amount_cents"], 100)
        self.store.compare_tax(self.sid, {"rows": [row(amount=300)]}, opts)
        current = self.store.tax_snapshot(self.sid, opts)[0]
        self.assertEqual(current["tax_amount_cents"], 300)
        self.assertFalse(current["db_comparison"]["missing"])

    def test_current_month_normal_growth_quiet_but_decrease_flagged(self):
        now = datetime(2026, 9, 20, tzinfo=timezone.utc).timestamp()
        opts = options_for("2026-09")
        with patch("backend.audit_store.time.time", return_value=now):
            for amount in (100, 400):
                self.store.compare_tax(self.sid, {"rows": [row(amount=amount)]}, opts)
            self.assertFalse(self.store.tax_snapshot(self.sid, opts)[0]["db_comparison"]["attention"])
            self.store.compare_tax(self.sid, {"rows": [row(amount=300)]}, opts)
            for _ in range(2):
                latest = self.store.tax_snapshot(self.sid, opts)[0]
                self.assertEqual(latest["tax_amount_cents"], 300)
                self.assertTrue(latest["db_comparison"]["attention"])
                self.assertTrue(any("减少" in w for w in latest["db_comparison"]["warnings"]))
                self.store.compare_tax(self.sid, {"rows": [row(amount=300)]}, opts)
            grouped = group_rows([{**latest, "shop_id": self.sid, "shop_name": "店铺"}])[0]
            self.assertTrue(grouped["db_changed"])

    def test_no_previous_amount_or_date_only_change_does_not_highlight(self):
        opts = options_for("2026-09")
        negative = {**row(), "refund_cents": 200, "refund": 2, "tax_amount_cents": -100, "tax_amount": -1}
        self.store.compare_tax(self.sid, {"rows": [negative]}, opts)
        self.assertFalse(self.store.tax_snapshot(self.sid, opts)[0]["db_comparison"]["attention"])
        earlier = {**row(), "summary_through": "2026-09-10"}
        self.store.compare_tax(self.sid, {"rows": [earlier]}, opts)
        self.assertFalse(
            any("截至日期" in w for w in self.store.tax_snapshot(self.sid, opts)[0]["db_comparison"]["warnings"])
        )
