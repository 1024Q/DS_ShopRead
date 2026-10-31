import copy
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend import collection, promotions
from backend.center.replication_worker import Delivery
from backend.center.source_delivery import describe
from backend.source_groups import summary_metadata
from backend.store import Store
from backend.system_status import snapshot, summary


class SummarySourceStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("fixture", "pw")
        self.options = promotions.options_for()
        self.key = "ad_reports:fixture"
        self.brief_key = "summary:" + self.key
        self.service = SimpleNamespace(
            store=self.store,
            session_getter=lambda _: SimpleNamespace(state="logged_in"),
            errors={}, progress={}, jobs={}, waiting_login=set(),
            queue_config=collection.settings(self.store), runtime_limits={},
        )
        collection.register(self.store, self.sid, self.key, "ad_reports", self.options)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, summary_only=False, on_demand=True):
        payload = {
            "rows": [] if summary_only else [{"id": "saved-detail"}],
            "source_total": 1,
            "complete": not summary_only,
            "metrics": {"ad_spend": 7 if summary_only else 1},
            "_sync": {"scope": self.options, "snapshot_started_at": time.time()},
        }
        if summary_only:
            payload.update(summary_complete=True, details_on_demand=on_demand)
        key = self.brief_key if summary_only else self.key
        self.store.write_cache(self.sid, key, payload)
        return self.store.read_cache(self.sid, key)

    def source(self, receipts=None):
        with patch("backend.center.source_delivery.load", return_value=receipts or {}):
            rows = snapshot(self.service)["sources"]
        self.assertNotIn("summary", {r["kind"] for r in rows})
        return next(r for r in rows if r["kind"] == "ad_reports")

    def test_summary_projects_fresh_clock_and_own_receipt_preserving_details(self):
        full = self.write()
        brief = self.write(summary_only=True)
        collection.finish(self.store, self.sid, self.key)
        saved = {"business": {
            "acks": {Delivery.key(self.sid, self.key): full["updated"], Delivery.key(self.sid, self.brief_key): brief["updated"]},
            "confirmed": {Delivery.key(self.sid, self.key): 1, Delivery.key(self.sid, self.brief_key): 2},
        }}
        for sharded in (False, True):
            if sharded:
                self.store.enable_shop_databases()
            row = self.source(saved)
            self.assertEqual(row["cache_key"], self.key)
            self.assertEqual(row["data_status"], "ready")
            self.assertFalse(row["stale"])
            self.assertTrue(row["details_on_demand"])
            self.assertTrue(row["details_complete"])
            self.assertFalse(row["complete"], "summary must not certify current detail rows")
            self.assertEqual(row["loaded_count"], 1)
            self.assertEqual(row["summary_updated_at"], brief["updated"])
            self.assertEqual(row["details_updated_at"], full["updated"])
            self.assertEqual(row["delivery"]["state"], "confirmed")
            self.assertEqual(row["delivery"]["local_at"], brief["updated"])
            self.assertEqual(row["delivery"]["confirmed_at"], 2)
            self.assertEqual(self.store.read_cache(self.sid, self.key), full)

    def test_summary_without_details_is_ready_and_not_a_second_cached_module(self):
        brief = self.write(summary_only=True)
        row = self.source()
        self.assertEqual(row["data_status"], "ready")
        self.assertEqual(row["loaded_count"], 0)
        self.assertFalse(row["details_complete"])
        self.assertIsNone(row["details_updated_at"])
        self.assertEqual(row["delivery"]["state"], "pending")
        self.assertEqual(row["delivery"]["local_at"], brief["updated"])
        cached = summary(self.service, task_group="cached")
        self.assertEqual(cached["total"], 1)
        self.assertIn("推广报表", cached["rows"][0]["module"])

    def test_later_full_snapshot_supersedes_summary_and_uses_full_receipt(self):
        self.write(summary_only=True)
        full = self.write()
        saved = {"business": {"acks": {Delivery.key(self.sid, self.key): full["updated"]}}}
        row = self.source(saved)
        self.assertTrue(row["complete"])
        self.assertFalse(row["details_on_demand"])
        self.assertEqual(row["delivery"]["state"], "confirmed")
        self.assertEqual(row["details_updated_at"], full["updated"])

    def test_interrupted_manual_detail_read_is_not_reported_as_success(self):
        self.write()
        self.write(summary_only=True, on_demand=False)
        collection.finish(self.store, self.sid, self.key, error="manual detail failed")
        row = self.source()
        self.assertEqual(row["data_status"], "error")
        self.assertIn("manual detail failed", row["error"])
        self.assertTrue(row["details_pending"])
        self.assertFalse(row["details_on_demand"])

    def test_receipt_for_older_full_does_not_confirm_new_summary(self):
        brief = {"updated": 20, "delivery_key": "summary:ad_reports:a", "delivery_origin": "business_cache"}
        saved = {"business": {"acks": {Delivery.key(1, "ad_reports:a"): 30}}}
        self.assertEqual(describe(saved, 1, "ad_reports:a", brief, {}, {})["state"], "pending")

    def test_metadata_merge_preserves_inputs_and_does_not_accept_incomplete_summary(self):
        rows = [
            {"shop_id": 1, "cache_key": "ad_reports:a", "updated": 1, "complete": True, "loaded_count": 4},
            {"shop_id": 1, "cache_key": "summary:ad_reports:a", "updated": 2, "summary_complete": False},
        ]
        before = copy.deepcopy(rows)
        self.assertEqual(summary_metadata(rows), rows[:1])
        self.assertEqual(rows, before)
