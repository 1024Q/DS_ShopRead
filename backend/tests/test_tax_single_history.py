import tempfile
import unittest
import json
from backend.store import Store
from backend.tests.test_audit_store import row
from backend.tax_info import options_for


class TaxSingleHistoryTests(unittest.TestCase):
    def test_existing_history_is_pruned_to_last_different_value(self):
        from backend.tax_review import initialize, append_snapshot

        with tempfile.TemporaryDirectory() as path:
            store = Store(path)
            sid = store.add("test", "pw")
            opts = options_for("2026-09")
            store.compare_tax(sid, {"rows": [row(amount=300)]}, opts)
            with store.connect() as db:
                version = db.execute("SELECT profile_version FROM shops WHERE id=?", (sid,)).fetchone()[0]
                for value in (100, 200, 300):
                    append_snapshot(db, sid, version, "2026-09", row(amount=value))
                db.execute("DELETE FROM settings WHERE key='tax_single_history_v1'")
            initialize(store)
            saved = store.tax_snapshot(sid, opts)[0]
            self.assertEqual(saved["db_comparison"]["previous"]["tax_amount_cents"], 200)
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM tax_history").fetchone()[0], 1)

    def test_official_month_and_quarter_are_not_combined(self):
        with tempfile.TemporaryDirectory() as path:
            store = Store(path)
            sid = store.add("test", "pw")
            with store.connect() as db:
                db.execute("UPDATE shops SET platform='jd' WHERE id=?", (sid,))
            month = {**row("2026-07"), "official_tax": True}
            quarter = {**month, "period": "2026-Q3", "id": "quarter"}
            store.compare_tax(sid, {"rows": [month]}, options_for("2026-07"))
            store.compare_tax(sid, {"rows": [quarter]}, options_for("2026-Q3"))
            self.assertEqual([r["period"] for r in store.tax_snapshot(sid, options_for("2026-Q3"))], ["2026-Q3"])

    def test_latest_previous_repeat_and_decrease(self):
        with tempfile.TemporaryDirectory() as path:
            store = Store(path)
            sid = store.add("test", "pw")
            opts = options_for("2026-09")
            for value in (100, 200, 150, 150):
                store.compare_tax(sid, {"rows": [row(amount=value)]}, opts)
            saved = store.tax_snapshot(sid, opts)[0]
            c = saved["db_comparison"]
            self.assertEqual(saved["tax_amount_cents"], 150)
            self.assertEqual(c["previous"]["tax_amount_cents"], 200)
            self.assertTrue(c["attention"])
            with store.connect() as db:
                history = db.execute("SELECT payload FROM tax_history").fetchall()
                self.assertEqual(len(history), 1)
                self.assertEqual(json.loads(history[0][0])["tax_amount_cents"], 200)
            store.compare_tax(sid, {"rows": [row(amount=180)]}, opts)
            self.assertFalse(store.tax_snapshot(sid, opts)[0]["db_comparison"]["attention"])

    def test_subject_only_company_and_official_quarter(self):
        with tempfile.TemporaryDirectory() as path:
            store = Store(path)
            sid = store.add("test", "pw")
            opts = options_for("2026-Q3")
            first = {**row("2026-07"), "official_tax": True, "period": "2026-Q3", "id": "old-name"}
            store.compare_tax(sid, {"rows": [first]}, opts)
            second = {**first, "entity": {**first["entity"], "credit_code": "NEW"}}
            store.compare_tax(sid, {"rows": [second]}, opts)
            self.assertFalse(store.tax_snapshot(sid, opts)[0]["db_comparison"]["attention"])
            third = {**second, "id": "new-name", "entity": {**second["entity"], "company_name": "新名称"}}
            store.compare_tax(sid, {"rows": [third]}, opts)
            saved = store.tax_snapshot(sid, opts)
            self.assertEqual(len(saved), 1)
            self.assertTrue(saved[0]["db_comparison"]["company_changed"])
            self.assertEqual(saved[0]["db_comparison"]["previous"]["entity"]["company_name"], "示例企业")
