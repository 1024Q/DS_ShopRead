import copy
import json
import tempfile
import unittest
from backend.store import Store
from backend.tax_info import options_for, group_rows


def row(period="2026-09", amount=100):
    return {
        "id": period,
        "period": period,
        "created_at": period + "-01",
        "name": "示例企业",
        "entity": {"company_name": "示例企业", "credit_code": "CODE1"},
        "summary_through": period + "-30",
        "partial": False,
        "trade_income_cents": amount,
        "coupon_subsidy_cents": 0,
        "refund_cents": 0,
        "coupon_refund_cents": 0,
        "tax_amount_cents": amount,
        "trade_income": amount / 100,
        "coupon_subsidy": 0,
        "refund": 0,
        "coupon_refund": 0,
        "tax_amount": amount / 100,
    }


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.shop = self.store.add("one", "pw")

    def tearDown(self):
        self.temp.cleanup()

    def test_month_and_quarter_share_latest_and_one_previous_value(self):
        opts = options_for("2026-09")
        first = self.store.compare_tax(self.shop, {"rows": [row()]}, opts)
        self.assertFalse(first["rows"][0]["db_comparison"]["changed"])
        changed = self.store.compare_tax(self.shop, {"rows": [row(amount=200)]}, options_for("2026-Q3"))
        diff = changed["rows"][0]["db_comparison"]["differences"]
        self.assertEqual((diff[0]["before"], diff[0]["after"]), (100, 200))
        repeated = Store(self.temp.name).compare_tax(self.shop, {"rows": [row(amount=200)]}, opts)
        self.assertTrue(repeated["rows"][0]["db_comparison"]["changed"])
        snapshot = Store(self.temp.name).tax_snapshot(self.shop, options_for("2026-Q3"))
        self.assertEqual(snapshot[0]["tax_amount_cents"], 200)
        self.assertTrue(snapshot[0]["db_comparison"]["changed"])
        with self.store.connect() as db:
            record = db.execute("SELECT * FROM tax_records").fetchone()
            self.assertEqual(json.loads(record["baseline"])["tax_amount_cents"], 100)
            self.assertEqual(json.loads(record["latest"])["tax_amount_cents"], 200)
        restored = self.store.compare_tax(self.shop, {"rows": [row()]}, opts)
        self.assertTrue(restored["rows"][0]["db_comparison"]["attention"])

    def test_entity_and_missing_records_are_highlighted_and_aggregate_retains_flag(self):
        self.store.compare_tax(self.shop, {"rows": [row()]}, options_for("2026-09"))
        latest = row()
        latest["entity"] = {**latest["entity"], "company_name": "新企业名称"}
        result = self.store.compare_tax(self.shop, {"rows": [latest]}, options_for("2026-09"))
        self.assertEqual(result["rows"][0]["db_comparison"]["differences"][0]["field"], "company_name")
        missing = self.store.compare_tax(self.shop, {"rows": []}, options_for("2026-09"))
        self.assertTrue(missing["rows"][0]["db_comparison"]["missing"])
        self.assertTrue(
            Store(self.temp.name).tax_snapshot(self.shop, options_for("2026-Q3"))[0]["db_comparison"]["missing"]
        )
        grouped = group_rows([{**missing["rows"][0], "shop_id": self.shop, "shop_name": "甲店"}])
        self.assertTrue(grouped[0]["db_changed"])
        self.assertTrue(grouped[0]["db_missing"])
        self.assertEqual(grouped[0]["tax_amount_cents"], 100)

    def test_profiles_do_not_compare_different_accounts(self):
        self.store.compare_tax(self.shop, {"rows": [row()]}, options_for("2026-09"))
        self.store.update(self.shop, "new-account", "pw2")
        result = self.store.compare_tax(self.shop, {"rows": [row(amount=200)]}, options_for("2026-09"))
        self.assertFalse(result["rows"][0]["db_comparison"]["changed"])

    def test_existing_saved_tax_data_is_migrated_as_baseline_once(self):
        self.store.write_cache(self.shop, "tax:old", {"rows": [row(amount=900)]})
        with self.store.connect() as db:
            db.execute("DELETE FROM settings WHERE key='tax_baseline_migrated'")
        reopened = Store(self.temp.name)
        result = reopened.compare_tax(self.shop, {"rows": [row(amount=1000)]}, options_for("2026-09"))
        self.assertEqual(result["rows"][0]["db_comparison"]["differences"][0]["before"], 900)
        self.assertEqual(
            Store(self.temp.name).compare_tax(self.shop, {"rows": [row(amount=1100)]}, options_for("2026-09"))["rows"][
                0
            ]["db_comparison"]["differences"][0]["before"],
            1000,
        )

    def test_file_logs_persist_filter_and_hide_secrets(self):
        for _ in range(2):
            self.store.log_event(
                "error",
                "tax",
                "read_error",
                "读取失败 password=do-not-show token=private https://example.test/?secret=x",
                self.shop,
            )
        self.store.log_event("info", "login", "success", "登录成功", self.shop)
        from backend.log_spool import get as log_spool

        log_spool(self.store).flush(self.store)
        logs = Store(self.temp.name).read_logs("error", self.shop, "读取失败")
        self.assertEqual(len(logs), 2)
        self.assertTrue(all(r["occurrences"] == 1 for r in logs))
        for secret in ("do-not-show", "private", "secret=x"):
            self.assertNotIn(secret, logs[0]["message"])
        from backend.log_spool import get as log_spool

        log_spool(self.store).flush(self.store)
        self.assertEqual(self.store.read_logs(keyword="not-found"), [])


class CurrentMonthTests(unittest.TestCase):
    def test_beijing_month_boundary_and_entity_or_missing_still_warn(self):
        from backend.audit_store import comparison, differences
        from datetime import datetime, timezone

        diff = differences(row(amount=100), row(amount=200))
        before = datetime(2026, 9, 30, 15, 59, tzinfo=timezone.utc).timestamp()
        after = datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc).timestamp()
        self.assertTrue(comparison("2026-09", diff, now=before)["routine_update"])
        self.assertFalse(comparison("2026-09", diff, now=before)["attention"])
        self.assertFalse(comparison("2026-09", diff, now=after)["attention"])
        entity_diff = differences(row(), {**row(), "entity": {"company_name": "新主体"}})
        self.assertTrue(comparison("2026-09", entity_diff, now=before)["attention"])
        self.assertFalse(comparison("2026-09", [], missing=True, now=before)["attention"])

    def test_current_month_retains_history_and_group_totals_without_warning(self):
        from unittest.mock import patch
        from datetime import datetime, timezone

        now = datetime(2026, 9, 20, tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as folder, patch("backend.audit_store.time.time", return_value=now):
            store = Store(folder)
            sid = store.add("one", "pw")
            opts = options_for("2026-09")
            store.compare_tax(sid, {"rows": [row()]}, opts)
            store.compare_tax(sid, {"rows": [row(amount=200)]}, opts)
            saved = store.tax_snapshot(sid, opts)[0]
            self.assertTrue(saved["db_comparison"]["changed"])
            self.assertTrue(saved["db_comparison"]["routine_update"])
            self.assertEqual(saved["db_comparison"]["differences"][0]["before"], 100)
            grouped = group_rows([{**saved, "shop_id": sid, "shop_name": "测试店"}])[0]
            self.assertFalse(grouped["db_changed"])
            self.assertTrue(grouped["current_month"])
            self.assertEqual(grouped["tax_amount_cents"], 200)
