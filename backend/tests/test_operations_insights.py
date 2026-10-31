import asyncio, json, tempfile, time, unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.store import Store
from backend import operations_insights as ops, attention, collection
from backend.business import BusinessService, options_for
from backend.business_reader import BEIJING
from backend.collection_queue import select_jobs
from backend.collection_errors import classify
from backend.database_maintenance import Maintenance
from backend.insights_api import router
from backend.admission import choose


class InsightsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("fixture", "pw")
        self.shop = self.store.list()[0]
        self.item = SimpleNamespace(state="logged_in", retired=False)
        self.service = BusinessService(self.store, lambda _: self.item)

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_day_partial_and_missing_are_not_zero(self):
        yesterday = datetime.now(BEIJING) - timedelta(days=1)
        self.store.write_cache(
            self.sid,
            "overview:test",
            {"metrics": {"sales": 0}, "complete": True, "_sync": {"snapshot_started_at": yesterday.timestamp()}},
        )
        self.store.write_cache(
            self.sid,
            "overview:test",
            {"metrics": {"sales": 123}, "complete": False, "_sync": {"snapshot_started_at": yesterday.timestamp() + 1}},
        )
        data = ops.history(self.store)
        self.assertEqual(data["rows"][-1]["sales"], 0)
        self.assertIsNone(data["rows"][0]["sales"])
        self.assertIsNone(data["total"])
        self.assertEqual(len(data["rows"]), 7)
        self.assertEqual(ops.history(self.store, 30)["days"], 30)
        self.assertEqual(ops.history(self.store, platform="taobao")["rows"][-1]["total"], 0)

    def test_history_historical_queries_do_not_overwrite_today(self):
        self.store.write_cache(
            self.sid,
            "transactions:test",
            {"metrics": {"sales": 123}, "complete": True, "_sync": {"scope": {"trade_range": "yesterday"}}},
        )
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM home_history").fetchone()[0], 0)

    def test_attention_baseline_changes_and_repeat_dedup(self):
        publish = lambda rows: attention.publish(self.store, self.sid, "evidence", {"rows": rows, "complete": True})
        publish([{"id": "1", "status": "待举证"}])
        self.assertEqual(ops.notifications(self.store, 0)["rows"], [])
        publish([{"id": "1", "status": "待举证"}, {"id": "2", "status": "待举证"}])
        publish([{"id": "1", "status": "待举证"}, {"id": "2", "status": "待举证"}])
        data = ops.notifications(self.store, 0)
        self.assertEqual([r["change"] for r in data["rows"]], ["new"])
        publish([{"id": "1", "status": "待举证"}])
        self.assertEqual(ops.notifications(self.store, data["cursor"])["rows"][0]["change"], "resolved")
        self.assertEqual(ops.notifications(self.store)["rows"], [])

    def test_health_uses_actual_samples_and_error_classification(self):
        ops.record_run(self.store, self.sid, "overview", time.time() - 2, True)
        ops.record_run(self.store, self.sid, "overview", time.time() - 4, False, "network")
        r = ops.health(self.service)["rows"][0]
        self.assertEqual(r["success_rate"], 50)
        self.assertEqual(r["runs"], 2)
        self.assertEqual(classify(TimeoutError()), ("network", False))
        self.assertEqual(classify(ValueError("没有当前模块的查看权限")), ("permission", True))

    def test_legacy_priority_is_ignored_and_running_limit_preserved(self):
        cfg = {**collection.DEFAULTS, "max_concurrent": 1, "realtime_concurrent": 1, "daily_concurrent": 1}
        job = lambda at, until=0: dict(kind="overview", phase="queued", queued_at=at, priority_until=until, scope={})
        progress = {(1, "overview"): job(980), (2, "overview"): job(990, 1010)}
        # The former hidden priority flag must not override the current due-time order.
        self.assertEqual(select_jobs(progress, cfg, now=1000)[0][0], 1)
        self.assertEqual(select_jobs(progress, cfg, now=1011)[0][0], 1)
        progress[(2, "overview")].update(phase="running")
        self.assertEqual(select_jobs(progress, cfg, now=1000), [])

    def test_midnight_admission_home_precedes_large_lists(self):
        now = datetime(2026, 10, 11, 0, 1, tzinfo=BEIJING).timestamp()
        last = now - 3600
        candidates = [(self.shop, self.item, k, {}, {"last_success": last}) for k in ("products", "overview")]
        selected, _, _ = choose(candidates, {}, collection.DEFAULTS, now)
        self.assertEqual(selected[0][2], "overview")

    def test_backup_verify_hash_and_preview_never_replace_live(self):
        m = Maintenance(self.store)
        info = m.create()
        self.store.add("new", "pw")
        result = m.verify_file(info["name"])
        self.assertEqual(result["counts"]["shops"], 1)
        self.assertEqual(len(self.store.list()), 2)
        with self.assertRaises(ValueError):
            m.verify_file("../shops")
        path = self.store.directory / "backups" / (info["name"] + ".sqlite3")
        with path.open("ab") as f:
            f.write(b"bad")
        with self.assertRaisesRegex(ValueError, "指纹"):
            m.verify_file(info["name"])

    def test_exact_retry_skips_success_and_preserves_saved_dates(self):
        app = FastAPI()
        app.include_router(router(self.service, lambda: None, Maintenance(self.store)))
        opts = options_for("reviews", date_from="2026-09-01", date_to="2026-09-02")
        opts["_exact_dates"] = True
        key = self.service.key("reviews", opts, self.shop)
        collection.register(self.store, self.sid, key, "reviews", opts)
        captured = []

        async def enqueue(shop, item, kind, options, **kwargs):
            captured.append(options)
            self.service.jobs[(shop["id"], self.service.key(kind, options, shop))] = True

        self.service.enqueue_async = enqueue
        with TestClient(app) as client:
            url = "/api/system/retry-scope"
            params = {"shop_id": self.sid, "cache_key": key}
            self.assertTrue(client.post(url, params=params).json()["skipped"])
            self.assertEqual(captured, [])
            collection.finish(self.store, self.sid, key, error="timeout")
            self.assertTrue(client.post(url, params=params).json()["queued"])
            self.assertEqual(captured, [opts])
            self.assertTrue(client.post(url, params=params).json()["skipped"])
            self.assertEqual(len(captured), 1)
            self.item.state = "stopped"
            self.assertEqual(client.post(url, params=params).status_code, 409)
            self.assertEqual(
                client.post("/api/system/priority", params={"shop_id": self.sid, "minutes": 16}).status_code, 422
            )
