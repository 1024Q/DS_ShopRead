import contextlib
import copy
import json
import sqlite3
import unittest
from datetime import datetime
from unittest.mock import patch

from backend.business import BusinessService
from backend.business_reader import BEIJING
from backend.task_step_metrics import combine, snapshot, _identity


class MetricStore:
    def __init__(self, shops):
        self.shops = shops
        self.reads = []
        self.databases = {}
        for shop in shops:
            db = sqlite3.connect(":memory:")
            db.row_factory = sqlite3.Row
            db.executescript("""
                CREATE TABLE business_cache(shop_id,cache_key,payload,updated);
                CREATE TABLE daily_archive(shop_id,cache_key,payload,updated);
                CREATE TABLE table_snapshots(shop_id,cache_key,metadata,updated);
            """)
            self.databases[shop["id"]] = db

    def list(self):
        return copy.deepcopy(self.shops)

    @contextlib.contextmanager
    def shop_db(self, shop_id, **kwargs):
        self.reads.append((shop_id, kwargs))
        try:
            yield self.databases[shop_id]
        finally:
            self.databases[shop_id].set_progress_handler(None, 0)

    def add(self, sid, kind, seconds, updated, *, scope=None, archive=False, summary=False, profile=None):
        shop = next(s for s in self.shops if s["id"] == sid)
        identity = {**shop, **({"profile_version": profile} if profile is not None else {})}
        scope = scope or {}
        key = BusinessService.key(None, kind, scope, identity)
        if summary:
            key = "summary:" + key
        payload = {"_sync": {"scope": scope, "duration_seconds": 900}}
        if seconds is not None:
            payload["_sync"]["read_seconds"] = seconds
        table = "daily_archive" if archive else "business_cache"
        self.databases[sid].execute(f"INSERT INTO {table} VALUES(?,?,?,?)", (sid, key, json.dumps(payload), updated))

    def close(self):
        for db in self.databases.values():
            db.close()


class StepMetricsTests(unittest.TestCase):
    def setUp(self):
        self.store = MetricStore(
            [
                {"id": 1, "platform": "pdd", "profile_version": 1},
                {"id": 2, "platform": "pdd", "profile_version": 0},
                {"id": 3, "platform": "taobao", "profile_version": 0},
            ]
        )
        self.addCleanup(self.store.close)

    def test_actual_read_timing_uses_latest_sample_per_shop_and_current_identity(self):
        self.store.add(1, "overview", 10, 100)
        self.store.add(1, "overview", 40, 90, archive=True)
        self.store.add(1, "overview", 999, 200, profile=0)
        self.store.add(2, "overview", 30, 110)
        self.store.add(3, "overview", 500, 120)
        self.store.add(1, "transactions", 7, 105, scope={"trade_range": "realtime"}, summary=True)
        self.store.add(1, "goodsdata", None, 106)
        result = snapshot(self.store, "pdd")
        overview = result["step_metrics"]["pdd"]["overview@base"]
        self.assertEqual(overview["sample_count"], 2)
        self.assertEqual(overview["estimated_read_seconds"], 20)
        self.assertEqual(overview["last_read_seconds"], 30)
        self.assertEqual(overview["sampled_at"], 110)
        self.assertEqual(result["step_metrics"]["pdd"]["transactions@base"]["last_read_seconds"], 7)
        self.assertNotIn("goodsdata@base", result["step_metrics"]["pdd"])
        self.assertEqual([sid for sid, _ in self.store.reads], [1, 2])
        self.assertTrue(all(not options.get("write") for _, options in self.store.reads))
        result["step_metrics"]["pdd"]["overview@base"]["sample_count"] = 999
        self.assertEqual(snapshot(self.store, "pdd")["step_metrics"]["pdd"]["overview@base"]["sample_count"], 2)
        self.assertEqual(len(self.store.reads), 2)

    def test_report_scopes_are_distinct_and_zero_is_a_real_measurement(self):
        self.store.add(1, "goodsdata", 0, 100, scope={"trade_range": "week"})
        self.store.add(1, "goodsdata", 15, 105, scope={"trade_range": "month"})
        self.store.add(1, "goodsdata", -5, 110)
        self.store.add(1, "goodsdata", 5, 115, scope={"trade_range": "quarter"})
        result = snapshot(self.store, "pdd")["step_metrics"]["pdd"]
        self.assertEqual(result["goodsdata@week"]["estimated_read_seconds"], 0)
        self.assertEqual(result["goodsdata@month"]["estimated_read_seconds"], 15)
        self.assertNotIn("goodsdata@base", result)
        self.assertNotIn("goodsdata@quarter", result)

    def test_bounded_read_marks_missing_nodes_instead_of_inventing_timings(self):
        with patch("backend.task_step_metrics.READ_BUDGET_SECONDS", 0):
            result = snapshot(self.store, "pdd")
        self.assertFalse(result["step_metrics_status"]["complete"])
        self.assertEqual(result["step_metrics_status"]["unavailable_shops"], 2)
        self.assertEqual(result["step_metrics"], {})
        self.assertEqual(self.store.reads, [])

    def test_old_recent_order_sample_keeps_its_original_day_class(self):
        stamp = datetime(2026, 10, 1, 12, tzinfo=BEIJING).timestamp()
        self.assertEqual(
            _identity("orders", {"_order_lane": "recent", "date_from": "2026-10-01"}, stamp), "orders@today"
        )
        self.assertEqual(
            _identity("orders", {"_order_lane": "recent", "date_from": "2026-09-30"}, stamp), "orders@yesterday"
        )

    def test_fleet_merge_is_weighted_and_uses_the_most_recent_sample(self):
        first = {
            "pdd": {
                "overview@base": dict(sample_count=2, estimated_read_seconds=10, last_read_seconds=8, sampled_at=100)
            }
        }
        second = {
            "pdd": {
                "overview@base": dict(sample_count=1, estimated_read_seconds=40, last_read_seconds=40, sampled_at=110)
            }
        }
        result = combine([first, second])["pdd"]["overview@base"]
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["estimated_read_seconds"], 20)
        self.assertEqual(result["last_read_seconds"], 40)


if __name__ == "__main__":
    unittest.main()
