import tempfile, time, unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from backend.store import Store
from backend.business import BusinessService, options_for
from backend.business_reader import BEIJING
from backend import collection, home_progress, system_status, runtime_state
from backend.query_proxy import local_read


class HomeProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("fixture", "pw")
        self.shop = self.store.list()[0]
        self.item = SimpleNamespace(state="logged_in", recovery_status="")
        self.service = BusinessService(self.store, lambda _: self.item)
        self.opts = options_for("overview")
        self.key = self.service.key("overview", self.opts, self.shop)

    def tearDown(self):
        self.temp.cleanup()

    def test_automatic_wake_keeps_coverage_and_overdue_denominator(self):
        before = home_progress.read(self.service)
        self.item.wanted_running = True
        self.item.collection_wake_task = SimpleNamespace(done=lambda: False)
        self.item.state = "logging_in"
        waking = home_progress.read(self.service)
        self.assertEqual(waking["total_modules"], before["total_modules"])
        self.assertEqual(waking["overdue_modules"], before["overdue_modules"])
        self.assertEqual(waking["rows"][0]["status"], "waking")
        self.assertTrue(all(m["overdue_reason"] == "waking" for m in waking["rows"][0]["modules"] if m["overdue"]))
        self.item.state = "needs_verification"
        self.assertEqual(home_progress.read(self.service)["total_modules"], 0)
        self.item.state = "logging_in"
        self.item.collection_wake_task = SimpleNamespace(done=lambda: True)
        self.assertEqual(home_progress.read(self.service)["total_modules"], 0)

    def test_running_and_persisting_remain_overdue_until_complete(self):
        for finished, reason in ((False, "running"), (True, "saving")):
            self.service.progress[(self.sid, self.key)] = {
                "phase": "running",
                "kind": "overview",
                "read_finished": finished,
            }
            module = next(m for m in home_progress.read(self.service)["rows"][0]["modules"] if m["kind"] == "overview")
            self.assertTrue(module["overdue"])
            self.assertEqual(module["overdue_reason"], reason)
            self.assertIsNone(module["data_age_seconds"])

    def test_successful_module_waiting_for_next_run_is_not_backlog(self):
        self.store.write_cache(self.sid, self.key, {"complete": True})
        collection.register(self.store, self.sid, self.key, "overview", self.opts)
        collection.finish(self.store, self.sid, self.key)
        self.service.deferred[(self.sid, self.key)] = {
            "kind": "overview",
            "stage": "等待下次执行",
            "next_run_at": time.time() + 60,
        }
        module = next(m for m in home_progress.read(self.service)["rows"][0]["modules"] if m["kind"] == "overview")
        self.assertEqual(module["status"], "updated")
        self.assertFalse(module["overdue"])
        self.assertEqual(module["stage"], "")

    def test_queued_home_module_reports_actual_capacity_blocker(self):
        self.service.progress[(self.sid, self.key)] = {"kind": "overview", "phase": "queued", "stage": "等待调度补位"}
        self.service.runtime_limits = {"max_concurrent": 1}
        self.service.progress[(self.sid, "transactions:running")] = {"kind": "transactions", "phase": "running"}
        module = next(m for m in home_progress.read(self.service)["rows"][0]["modules"] if m["kind"] == "overview")
        self.assertEqual(module["status"], "queued")
        self.assertEqual(module["stage"], "采集并发已满")

    def test_covered_queued_demand_does_not_repeat_until_due(self):
        from backend.task_lifecycle import before_run

        collection.register(self.store, self.sid, self.key, "overview", self.opts)

        def allowed(force=False):
            return before_run(self.store, self.shop, "overview", self.opts, self.key, self.service.key, force, True)

        self.assertTrue(allowed())
        collection.finish(self.store, self.sid, self.key)
        self.assertFalse(allowed())
        self.assertTrue(allowed(True))
        with self.store.connect() as db:
            db.execute(
                "UPDATE collection_tasks SET continuation=1 WHERE shop_id=? AND cache_key=?", (self.sid, self.key)
            )
        self.assertTrue(allowed())
        with self.store.connect() as db:
            db.execute(
                "UPDATE collection_tasks SET continuation=0,last_success=? WHERE shop_id=? AND cache_key=?",
                (time.time() - 86400, self.sid, self.key),
            )
        self.assertTrue(allowed())

    def test_today_progress_scope_and_partial_data(self):
        yesterday = (datetime.now(BEIJING) - timedelta(days=1)).timestamp()
        self.store.write_cache(
            self.sid, self.key, {"metrics": {"sales": 2}, "complete": True, "_sync": {"snapshot_started_at": yesterday}}
        )
        report = home_progress.read(self.service)
        self.assertEqual(report["updated_modules"], 0)
        self.assertEqual(report["total_shops"], 1)
        self.assertEqual(report["total_modules"], 12)
        self.assertIsNotNone(report["last_updated"])
        self.assertFalse(self.service.jobs)
        self.store.write_cache(self.sid, self.key, {"metrics": {"sales": 2}, "complete": True})
        self.assertEqual(home_progress.read(self.service)["updated_modules"], 1)
        self.store.write_cache(self.sid, self.key, {"complete": False})
        self.assertEqual(home_progress.read(self.service)["updated_modules"], 0)
        self.assertEqual(home_progress.read(self.service, platform="taobao")["rows"], [])
        self.assertTrue(local_read("GET", "/api/home/progress"))

    def test_runtime_error_login_stopped_and_offline(self):
        collection.register(self.store, self.sid, self.key, "overview", self.opts)
        collection.finish(self.store, self.sid, self.key, error="测试异常")
        self.assertEqual(home_progress.read(self.service)["rows"][0]["status"], "retry_wait")
        self.service.progress[(self.sid, self.key)] = {"phase": "running", "kind": "overview"}
        self.assertEqual(home_progress.read(self.service)["rows"][0]["status"], "running")
        self.service.progress.clear()
        self.item.state = "needs_verification"
        self.assertEqual(home_progress.read(self.service)["rows"][0]["status"], "login")
        self.item.state = "stopped"
        self.assertEqual(home_progress.read(self.service)["rows"][0]["status"], "stopped")
        source = next(s for s in system_status.snapshot(self.service)["sources"] if s["kind"] == "overview")
        self.assertEqual(source["data_status"], "stopped")
        self.assertIsNone(source["error"])
        self.assertEqual(source["previous_error"], "测试异常")
        self.assertFalse(source["retry_enabled"])
        self.assertEqual(system_status.summary(self.service)["summary"]["errors"], 0)
        runtime_state.save(self.store, self.service, {self.sid: self.item})
        runtime_state.load(self.store, self.service, {})
        self.assertEqual(self.service.public_summary["summary"]["errors"], 0)
        self.service.public_summary["collector_online"] = False
        self.assertEqual(home_progress.read(self.service)["rows"][0]["status"], "offline")

    def test_overdue_uses_frequency_and_success_not_partial_write(self):
        now = datetime.now(BEIJING).replace(hour=12, minute=0, second=0, microsecond=0)
        collection.save_settings(self.store, {"settings_patch": {"group_updates": {"custom": {"seconds": 300}}}})
        collection.save_settings(
            self.store,
            {"module_assignments": [{"tab": "task", "platform": "pdd", "kind": "overview@home", "target": "custom"}]},
        )
        self.store.write_cache(self.sid, self.key, {"complete": True})
        collection.register(self.store, self.sid, self.key, "overview", self.opts)

        def report(age):
            with self.store.connect() as db:
                db.execute(
                    "UPDATE collection_tasks SET last_success=? WHERE shop_id=? AND cache_key=?",
                    (now.timestamp() - age, self.sid, self.key),
                )
            data = home_progress.read(self.service, now=now)
            return data, next(m for m in data["rows"][0]["modules"] if m["kind"] == "overview")

        data, module = report(299)
        self.assertFalse(module["overdue"])
        self.assertIsNotNone(data["last_collected"])
        data, module = report(301)
        self.assertTrue(module["overdue"])
        self.assertGreater(data["overdue_modules"], 0)
        self.assertEqual(module["status"], "waiting")
        self.assertIn("自动排队", module["stage"])
        self.store.write_cache(self.sid, self.key, {"complete": False})
        data, module = report(301)
        self.assertTrue(module["overdue"])
        self.service.progress[(self.sid, self.key)] = {"phase": "running"}
        data, module = report(301)
        self.assertTrue(module["overdue"])
        collection.save_settings(self.store, {"enabled": False})
        data, module = report(301)
        self.assertFalse(module["overdue"])
        self.assertEqual(data["overdue_modules"], 0)

    def test_daily_schedule_and_attention_frequency_match_scheduler(self):
        now = datetime.now(BEIJING).replace(hour=2, minute=0, second=0, microsecond=0)
        config = {**collection.DEFAULTS, "daily_time": "03:00", "realtime_seconds": 180}
        yesterday = (now - timedelta(days=1)).timestamp()
        self.assertFalse(collection.due("products", {}, yesterday, config, now.timestamp()))
        self.assertTrue(collection.due("products", {}, yesterday, config, (now + timedelta(hours=2)).timestamp()))
        self.assertFalse(
            collection.due("tickets", {"_attention": True}, now.timestamp() - 179, config, now.timestamp())
        )
        self.assertTrue(collection.due("tickets", {"_attention": True}, now.timestamp() - 181, config, now.timestamp()))

    def test_failed_today_snapshot_waits_for_retry_without_losing_coverage(self):
        self.store.write_cache(self.sid, self.key, {"complete": True})
        collection.register(self.store, self.sid, self.key, "overview", self.opts)
        collection.finish(self.store, self.sid, self.key, error="读取超时")
        with self.store.connect() as db:
            db.execute(
                "UPDATE collection_tasks SET last_success=?,retry_at=? WHERE shop_id=? AND cache_key=?",
                (time.time() - 600, time.time() - 1, self.sid, self.key),
            )
        self.service.deferred[(self.sid, self.key)] = {"kind": "overview"}

        def module():
            return next(m for m in home_progress.read(self.service)["rows"][0]["modules"] if m["kind"] == "overview")

        m = module()
        self.assertEqual(m["status"], "deferred")
        self.assertTrue(m["today_updated"])
        self.assertTrue(m["overdue"])
        self.assertEqual(m["error"], "读取超时")
        with self.store.connect() as db:
            db.execute("UPDATE collection_tasks SET blocked=1 WHERE shop_id=? AND cache_key=?", (self.sid, self.key))
        self.assertEqual(module()["status"], "error")
        self.service.deferred.clear()
        collection.finish(self.store, self.sid, self.key)
        m = module()
        self.assertEqual(m["status"], "updated")
        self.assertFalse(m["overdue"])
        self.assertFalse(m["error"])
