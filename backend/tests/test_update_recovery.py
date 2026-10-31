import asyncio, inspect, sqlite3, tempfile, threading, time, unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, Mock
from backend.store import Store
from backend import collection
from backend.async_database import record_state
from backend.machine_load import LoadController
from backend.collector_update import healthy
import httpx


class RecoveryTests(unittest.TestCase):
    def test_log_lock_falls_back_without_interrupting_task(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            with store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                store.log_event("error", "orders", "sync_error", "lock test")
            logs = list((Path(folder) / "log-spool").glob("*.jsonl"))
            self.assertEqual(len(logs), 1)
            self.assertIn("lock test", logs[0].read_text(encoding="utf-8"))

    def test_parallel_failure_updates_are_not_lost(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            collection.register(store, 1, "orders:x", "orders", {})
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: collection.finish(store, 1, "orders:x", error="test"), range(16)))
            self.assertEqual(collection.state(store, 1, "orders:x")["failures"], 16)

    def test_state_lock_retry_is_bounded(self):
        operation = Mock(side_effect=[sqlite3.OperationalError("database is locked"), None])
        self.assertTrue(asyncio.run(record_state(operation)))
        self.assertEqual(operation.call_count, 2)
        operation = Mock(side_effect=sqlite3.OperationalError("database is locked"))
        self.assertFalse(asyncio.run(record_state(operation)))
        self.assertEqual(operation.call_count, 3)

    def test_warmup_preserves_configured_limit(self):
        with patch.dict("os.environ", {"ECMP_UPDATE_WARMUP_SECONDS": "300"}):
            controller = LoadController()
        c = {
            **collection.DEFAULTS,
            "max_concurrent": 16,
            "realtime_concurrent": 14,
            "daily_concurrent": 2,
            "adaptive_enabled": False,
        }
        for seconds, expected in ((0, 2), (30, 4), (90, 8), (300, 16)):
            r = controller.update(c, now=controller.started + seconds, measurements=(20, 30))
            self.assertEqual(r["max_concurrent"], expected)
            self.assertEqual(controller.status["configured_limit"], 16)

    def test_health_distinguishes_timeout_and_exit(self):
        runner = Mock(running=True, children={"collector": Mock(poll=Mock(return_value=None))}, port=8001)
        with patch("backend.collector_update.httpx.get", side_effect=httpx.ReadTimeout("slow")):
            self.assertFalse(healthy(runner))
        self.assertIn("超时", runner.health_issue)
        runner.children["collector"].poll.return_value = 1
        self.assertFalse(healthy(runner))
        self.assertIn("退出码 1", runner.health_issue)
