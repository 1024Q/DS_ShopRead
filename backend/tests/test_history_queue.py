import unittest
from unittest.mock import patch
from backend import collection
from backend.collection_queue import blocking_task, waiting_reason, select_jobs
from backend.demand_refresh import timing


class HistoryQueueTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            **collection.DEFAULTS,
            "frequency_version": 1,
            "max_concurrent": 1,
            "shop_concurrent": 3,
            "frequency_groups": [{"id": "custom", "name": "常规", "seconds": 60, "time": "", "timeout": 60}],
            "task_modes": {},
            "platform_modes": {},
        }

    def test_blocker_is_exact_same_shop_parent_module_and_shows_elapsed(self):
        queued = {"phase": "queued", "kind": "ad_reports", "scope": {"trade_range": "realtime"}, "queued_at": 90}
        progress = {
            (1, "orders"): dict(queued, phase="running", kind="orders", started_at=10),
            (2, "ad_reports"): dict(queued, phase="running", started_at=20),
            (1, "ad_reports:week"): dict(queued, phase="running", scope={"trade_range": "week"}, started_at=40),
            (1, "ad_reports:today"): queued,
        }
        with patch("backend.collection_queue.time.time", return_value=100):
            blocker = blocking_task((1, "ad_reports:today"), queued, progress)
            self.assertEqual(blocker["scope"], {"trade_range": "week"})
            self.assertEqual(blocker["elapsed_seconds"], 60)
            reason = waiting_reason((1, "ad_reports:today"), queued, progress, self.config)
            self.assertIn("同模块", reason)
            self.assertIn("近7天数据", reason)
            self.assertIn("已运行 60 秒", reason)
        progress.pop((1, "ad_reports:week"))
        self.assertIsNone(blocking_task((1, "ad_reports:today"), queued, progress, 100))

    def test_database_wait_does_not_claim_unrelated_module_blocker(self):
        queued = {"phase": "queued", "kind": "ad_reports", "database_pressure": True}
        progress = {(1, "ad_reports"): dict(queued, phase="running")}
        self.assertIsNone(blocking_task((1, "ad_reports:today"), queued, progress))
        self.assertEqual(waiting_reason((1, "ad_reports:today"), queued, progress, self.config), "等待入库队列缓解")

    def test_history_rejoins_after_existing_waiter_without_resetting_generation(self):
        history = {
            "kind": "ad_reports",
            "phase": "queued",
            "scope": {"trade_range": "month"},
            "platform": "pdd",
            "queued_at": 10,
            "waiting_since": 10,
            "continuation": True,
            "queue_turn_at": 100,
        }
        today = {
            **history,
            "scope": {"trade_range": "realtime"},
            "queued_at": 80,
            "waiting_since": 80,
            "continuation": False,
        }
        progress = {(1, "ad_reports:month"): history, (1, "ad_reports:today"): today}
        self.assertEqual(select_jobs(progress, self.config, now=100), [(1, "ad_reports:today")])
        self.assertEqual(timing("ad_reports", history["scope"], history, self.config, "pdd", 100)["generated_at"], 10)
        progress.pop((1, "ad_reports:today"))
        self.assertEqual(select_jobs(progress, self.config, now=100), [(1, "ad_reports:month")])

    def test_relative_history_from_previous_day_cannot_resume_under_old_identity(self):
        from datetime import datetime, timedelta
        from backend.business_reader import BEIJING
        from backend.collection_plan import obsolete_realtime
        from backend.task_lifecycle import retained

        today = datetime.now(BEIJING).date()
        yesterday = today - timedelta(days=1)
        for kind, date_key, current in [
            ("goodsdata", "day", today),
            ("transactions", "day", today),
            ("ad_reports", "date_to", yesterday),
            ("ad_products", "date_to", yesterday),
        ]:
            scope = {"trade_range": "month", date_key: current.isoformat()}
            self.assertFalse(obsolete_realtime(kind, scope))
            stale = {**scope, date_key: (current - timedelta(days=1)).isoformat()}
            self.assertTrue(obsolete_realtime(kind, stale))
            self.assertFalse(retained({"kind": kind, "continuation": True}, stale, fixed=True))
            self.assertFalse(obsolete_realtime(kind, {**stale, "_exact_dates": True}))
            self.assertFalse(obsolete_realtime(kind, {**stale, "explicit_dates": True}))
