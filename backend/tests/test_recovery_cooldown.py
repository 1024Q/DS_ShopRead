import asyncio
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend import collection
from backend.store import Store
from backend.browser_recovery import check, restored
from backend.collection_recovery import browser_restored, watchdog
from backend.shop_lock import ShopLock


class RecoveryCooldownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("test", "pw")

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, key, category, *, attempt=100, lease=0, blocked=False):
        collection.register(self.store, self.sid, key, "overview", {})
        with self.store.write_connection() as db:
            db.execute(
                "UPDATE collection_tasks SET error_kind=?,last_attempt=?,lease_until=?,blocked=?,retry_at=?,last_success=50,failures=5,continuation=1 WHERE cache_key=?",
                (category, attempt, lease, int(blocked), time.time() + 1800, key),
            )

    def test_browser_recovery_only_releases_prior_connection_and_login_failures(self):
        for key, category, opts in [
            ("network", "network", {}),
            ("login", "login", {"blocked": True}),
            ("limit", "rate_limit", {}),
            ("db", "database_busy", {}),
            ("permission", "permission", {"blocked": True}),
            ("active", "network", {"lease": 999}),
            ("new", "network", {"attempt": 201}),
        ]:
            self.task(key, category, **opts)
        self.assertEqual(browser_restored(self.store, self.sid, 200), 2)
        for key in ("network", "login"):
            task = collection.state(self.store, self.sid, key)
            self.assertEqual(task["retry_at"], 0)
            self.assertFalse(task["blocked"])
            self.assertEqual(task["last_success"], 50)
            self.assertTrue(task["continuation"])
            self.assertEqual(task["failures"], 5)
        for key in ("limit", "db", "permission", "active", "new"):
            self.assertGreater(collection.state(self.store, self.sid, key)["retry_at"], time.time())

    def test_failure_categories_have_separate_cooldowns(self):
        for category, low, high in [("network", 299, 300), ("database_busy", 59, 60), ("rate_limit", 3599, 3600)]:
            self.task(category, category)
            collection.finish(self.store, self.sid, category, error="failure", error_kind=category)
            row = collection.state(self.store, self.sid, category)
            self.assertEqual(row["error_kind"], category)
            self.assertTrue(low < row["retry_at"] - time.time() <= high)
            self.assertEqual(row["last_success"], 50)
            self.assertTrue(row["continuation"])
        collection.register(self.store, self.sid, "limited", "overview", {})
        collection.finish(self.store, self.sid, "limited", error="请求频繁，请稍后重试")
        row = collection.state(self.store, self.sid, "limited")
        self.assertEqual(row["error_kind"], "rate_limit")
        self.assertGreater(row["retry_at"] - time.time(), 299)

    async def test_healthy_poll_does_not_erase_module_failure_backoff(self):
        self.task("network", "network")
        item = SimpleNamespace(
            retired=False,
            wanted_running=True,
            lock=ShopLock(),
            state="logged_in",
            refresh=AsyncMock(),
            start=AsyncMock(),
            store=self.store,
            shop={"id": self.sid},
            error="",
        )
        await check(item)
        self.assertGreater(collection.state(self.store, self.sid, "network")["retry_at"], time.time())
        item.start.assert_not_awaited()

    async def test_recovery_metadata_busy_does_not_restart_browser_and_is_retried(self):
        self.task("network", "network")
        item = SimpleNamespace(
            retired=False,
            wanted_running=True,
            lock=ShopLock(),
            state="logged_in",
            refresh=AsyncMock(),
            start=AsyncMock(),
            store=self.store,
            shop={"id": self.sid},
            error="",
        )
        with patch(
            "backend.collection_recovery.browser_restored", side_effect=sqlite3.OperationalError("database locked")
        ):
            await restored(item, 200)
        self.assertEqual(item.pending_collection_recovery_at, 200)
        await check(item)
        item.start.assert_not_awaited()
        self.assertEqual(item.pending_collection_recovery_at, 0)
        self.assertEqual(collection.state(self.store, self.sid, "network")["retry_at"], 0)

    async def test_watchdog_uses_read_deadline_and_does_not_cancel_database_work(self):
        metas = [{"timeout_paused": True}, {"read_finished": True}, {"read_deadline": 1100}, {"read_deadline": 900}]
        tasks = [asyncio.create_task(asyncio.sleep(100)) for _ in metas]
        service = SimpleNamespace(
            store=self.store,
            wake=asyncio.Event(),
            jobs={(self.sid, str(i)): t for i, t in enumerate(tasks)},
            progress={
                (self.sid, str(i)): {
                    "phase": "running",
                    "kind": "overview",
                    "started_at": 1,
                    "timeout_seconds": 40,
                    **meta,
                }
                for i, meta in enumerate(metas)
            },
        )
        try:
            await watchdog(service, now=1000)
            self.assertEqual([bool(t.cancelling()) for t in tasks], [False, False, False, True])
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
