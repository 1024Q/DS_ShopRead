import asyncio, tempfile, time, unittest
from unittest.mock import AsyncMock, patch
from backend import collection
from backend.store import Store
from backend.browser import Session
from backend.business import BusinessService, options_for
from backend.scheduler import Scheduler
from backend.system_status import snapshot
from backend.tests.collection_fixture import assign


class RetryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("retry", "pw")
        self.shop = self.store.list()[0]
        self.item = Session(self.shop, self.store)
        self.item.state = "logged_in"
        self.item.window = AsyncMock()
        self.service = BusinessService(self.store, lambda _: self.item)
        self.scheduler = Scheduler(self.service)
        from backend.tests.collection_fixture import assign

        collection.save_settings(self.store, {"settings_patch": {"keepalive_enabled": False}})
        assign(self.store, {"products@base": "daily"}, reset=True)
        self.options = options_for("products")
        self.key = self.service.key("products", self.options, self.shop)

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    async def drain(self):
        # Batched discovery queues one dispatcher callback instead of creating
        # a worker synchronously for every admitted plan.
        await asyncio.sleep(0)
        if self.service.jobs:
            await asyncio.gather(*list(self.service.jobs.values()))
            await asyncio.sleep(0)

    async def test_failed_daily_refresh_retries_today_and_resets_progress(self):
        payload = {"rows": [], "complete": True}
        with patch("backend.business.collect", new=AsyncMock(return_value=payload)):
            self.service.request_sync("products", self.options)
            await self.drain()
        with self.store.connect() as db:
            db.execute("UPDATE collection_tasks SET last_success=last_success-11")
            db.execute("UPDATE business_cache SET updated=updated-11")
        with patch("backend.business.collect", new=AsyncMock(side_effect=ValueError("平台返回异常"))):
            self.service.request_sync("products", self.options, force=True)
            await self.drain()
        state = collection.state(self.store, self.sid, self.key)
        self.assertGreater(state["retry_at"], time.time())
        self.assertLessEqual(state["retry_at"] - time.time(), 60)
        with patch("backend.business.collect", new=AsyncMock(return_value=payload)) as read:
            await self.scheduler.tick()
            await self.scheduler.refill()
            await self.drain()
            read.assert_not_awaited()
            with self.store.connect() as db:
                db.execute("UPDATE collection_tasks SET retry_at=0")
            await self.scheduler.tick()
            await self.scheduler.refill()
            await self.drain()
            read.assert_awaited_once()
        state = collection.state(self.store, self.sid, self.key)
        self.assertEqual(state["failures"], 0)
        self.assertEqual(state["last_error"], "")
        self.assertEqual(state["lease_until"], 0)
        self.assertEqual(self.service.jobs, {})
        self.assertEqual(self.service.progress, {})

    async def test_failure_backoff_is_capped_and_paused_module_stays_paused(self):
        collection.register(self.store, self.sid, self.key, "products", self.options)
        for _ in range(8):
            collection.finish(self.store, self.sid, self.key, error="error")
        state = collection.state(self.store, self.sid, self.key)
        self.assertTrue(3500 < state["retry_at"] - time.time() <= 3600)
        self.assertFalse(collection.claim(self.store, self.sid, self.key))
        with self.store.connect() as db:
            db.execute("UPDATE collection_tasks SET retry_at=0")
        assign(self.store, {"products@base": "off"})
        self.assertFalse(collection.claim(self.store, self.sid, self.key))
        await self.scheduler.tick()
        self.assertFalse(self.service.jobs)

    async def test_status_exposes_retry_deadline_and_current_attempt(self):
        collection.register(self.store, self.sid, self.key, "products", self.options)
        collection.finish(self.store, self.sid, self.key, error="读取失败")
        row = next(r for r in snapshot(self.service)["sources"] if r["kind"] == "products")
        self.assertEqual(row["failures"], 1)
        self.assertTrue(0 < row["retry_wait_seconds"] <= 60)
        job = (self.sid, self.key)
        self.service.progress[job] = {"phase": "running", "loaded": 5, "total": 10, "scope": self.options}
        row = next(r for r in snapshot(self.service)["sources"] if r["kind"] == "products")
        self.assertEqual(row["data_status"], "running")
        self.assertIsNone(row["error"])
        self.assertEqual(row["previous_error"], "读取失败")
        self.assertEqual(row["sync_loaded"], 5)

    async def test_one_shop_scheduler_error_does_not_skip_other_shops(self):
        self.store.add("other", "pw")
        with patch.object(
            self.scheduler, "tick_shop", new=AsyncMock(side_effect=[ValueError("bad scope"), None])
        ) as tick:
            await self.scheduler.tick()
            self.assertEqual(tick.await_count, 2)

    async def test_expired_filtered_request_retires_without_retrying_stale_query(self):
        assign(self.store, {"reviews@base": "daily"})
        options = {**options_for("reviews", stars="1"), "_filtered_read": True}
        key = self.service.key("reviews", options, self.shop)
        collection.register(self.store, self.sid, key, "reviews", options)
        collection.finish(self.store, self.sid, key, error="连接超时")
        with self.store.connect() as db:
            db.execute("UPDATE collection_tasks SET demand_until=0,retry_at=0 WHERE cache_key=?", (key,))
        with patch("backend.business.collect", new=AsyncMock(return_value={"rows": [], "complete": True})) as read:
            await self.scheduler.tick()
            await self.scheduler.refill()
            await self.drain()
        self.assertEqual(collection.state(self.store, self.sid, key)["schedule_state"], "archived")
        self.assertFalse(any(call.args[2].get("stars") == [1] for call in read.await_args_list))

    async def test_all_failure_ranges_expose_earliest_repair_deadline(self):
        assign(self.store, {"reviews@base": "daily"})
        for stars in ("1", "2"):
            options = {**options_for("reviews", stars=stars), "_filtered_read": True}
            key = self.service.key("reviews", options, self.shop)
            collection.register(self.store, self.sid, key, "reviews", options)
            collection.finish(self.store, self.sid, key, error="超时")
        row = next(r for r in snapshot(self.service)["sources"] if r["kind"] == "reviews")
        self.assertEqual(row["repair_auto_count"], 2)
        self.assertEqual(row["repair_retry_at"], min(f["retry_at"] for f in row["failed_scopes"]))
        self.assertGreater(row["repair_retry_at"], row["retry_server_time"])
