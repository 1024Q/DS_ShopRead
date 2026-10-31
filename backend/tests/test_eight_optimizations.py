import asyncio, json, sqlite3, tempfile, time, unittest
from contextlib import closing
from unittest.mock import AsyncMock, patch
from backend import collection, collection_checkpoint as cp
from backend.store import Store
from backend.browser import Session
from backend.business import BusinessService, options_for
from backend.collection_errors import classify
from backend.read_errors import PageReadError, LoginPending
from backend.machine_load import LoadController
from backend.database_maintenance import Maintenance
from backend.taobao_ads import campaign
from backend.tests.collection_fixture import assign


class CheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("test", "pw")
        self.key = "reviews:test"
        collection.register(self.store, self.sid, self.key, "reviews", options_for("reviews"))

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def fetcher(self, ids, size=100):
        async def fetch(page):
            batch = ids[(page - 1) * size : page * size]
            return {
                "rows": [{"id": str(x), "value": x} for x in batch],
                "ids": [str(x) for x in batch],
                "total": len(ids),
                "has_more": page * size < len(ids),
            }

        return fetch

    async def test_more_than_2000_survives_restart_without_publishing_partial(self):
        ids = list(range(2505))
        old = {"rows": [{"id": "old"}], "complete": True}
        self.store.write_cache(self.sid, self.key, old)
        batches = 0
        while True:
            batches += 1
            with cp.context(self.store, self.sid, self.key, {"batch_pages": 3, "batch_seconds": 60}):
                try:
                    result = await cp.paginate("test", self.fetcher(ids), lambda *_: None)
                    break
                except cp.YieldCollection:
                    pass
            self.assertEqual(self.store.read_cache(self.sid, self.key)["payload"], old)
            self.store = Store(self.tmp.name)
            self.assertLess(batches, 20)
        self.assertGreater(batches, 1)
        self.assertEqual(len(result["rows"]), 2505)
        self.assertEqual({r["id"] for r in result["rows"]}, {str(i) for i in ids})
        self.assertTrue(result["complete"])

    async def test_changed_head_restarts_instead_of_skipping_insertions(self):
        with cp.context(self.store, self.sid, self.key, {"batch_pages": 2}):
            with self.assertRaises(cp.YieldCollection):
                await cp.paginate("test", self.fetcher(list(range(400))), lambda *_: None)
        with cp.context(self.store, self.sid, self.key, {"batch_pages": 50}):
            result = await cp.paginate("test", self.fetcher([-1, *range(400)]), lambda *_: None)
        self.assertEqual(len(result["rows"]), 401)
        self.assertIn("-1", {r["id"] for r in result["rows"]})

    async def test_repeated_page_does_not_advance_saved_cursor(self):
        async def fetch(n):
            return {"rows": [{"id": "a"}], "ids": ["a"], "total": 3}

        with cp.context(self.store, self.sid, self.key, {"batch_pages": 10}) as checkpoint:
            with self.assertRaises(PageReadError):
                await cp.paginate("test", fetch, lambda *_: None)
            self.assertEqual(next(iter(checkpoint.data["parts"].values()))["next"], 2)

    async def test_completed_parts_do_not_consume_each_resumed_batch(self):
        for attempt in range(8):
            with cp.context(self.store, self.sid, self.key, {"batch_pages": 2}):
                try:
                    for part in range(4):
                        await cp.paginate(str(part), self.fetcher(list(range(3)), 1), lambda *_: None)
                    break
                except cp.YieldCollection:
                    pass
        else:
            self.fail("batch cannot progress beyond earlier completed parts")


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("test", "pw")
        self.item = Session(self.store.list()[0], self.store)
        self.item.state = "logged_in"
        self.item.window = AsyncMock()
        self.service = BusinessService(self.store, lambda _: self.item)
        assign(self.store, {"reviews@base": "custom", "brands@base": "custom"})

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    async def drain(self):
        from backend.tests.collection_fixture import drain

        await drain(self.service)

    async def test_permission_stops_auto_retry_manual_retry_can_recover(self):
        opts = options_for("brands")
        key = self.service.key("brands", opts, self.item.shop)
        with patch("backend.business.collect", new=AsyncMock(side_effect=ValueError("该店铺没有当前模块的查看权限"))):
            self.service.request_sync("brands", opts)
            await self.drain()
        state = collection.state(self.store, self.sid, key)
        self.assertTrue(state["blocked"])
        self.assertEqual(state["error_kind"], "permission")
        with self.store.connect() as db:
            db.execute("UPDATE collection_tasks SET retry_at=0")
        with patch("backend.business.collect", new=AsyncMock(return_value={"rows": [], "complete": True})) as reader:
            self.service.request_sync("brands", opts)
            self.assertFalse(self.service.jobs)
            self.service.request_sync("brands", opts, force=True)
            await self.drain()
            reader.assert_awaited_once()
        self.assertFalse(collection.state(self.store, self.sid, key)["blocked"])

    async def test_running_background_request_remembers_one_manual_rerun(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def collect(*args):
            calls.append(1)
            started.set()
            if len(calls) == 1:
                await release.wait()
            return {"rows": [], "complete": True}

        opts = options_for("overview")
        with patch("backend.business.collect", new=collect):
            self.service.request_sync("overview", opts)
            await asyncio.wait_for(started.wait(), 3)
            for _ in range(4):
                self.service.request_sync("overview", opts, force=True)
            release.set()
            await self.drain()
        self.assertEqual(len(calls), 2)

    async def test_yield_releases_shop_without_marking_failure(self):
        opts = options_for("reviews")
        key = self.service.key("reviews", opts, self.item.shop)

        async def collect(*args):
            cp.current.get().save()
            # Inspect this incomplete checkpoint before allowing another segment.
            self.service.queue_config['enabled'] = False
            raise cp.YieldCollection()

        with patch("backend.business.collect", new=collect):
            self.service.request_sync("reviews", opts)
            await self.drain()
        state = collection.state(self.store, self.sid, key)
        self.assertEqual(state["failures"], 0)
        self.assertTrue(state["continuation"])
        self.assertFalse(state["lease_until"])
        self.assertFalse(self.item.lock.locked())
        self.assertFalse(self.service.running_shops)

    async def test_successful_retry_batch_clears_old_error_without_publishing_success(self):
        opts = options_for("reviews")
        key = self.service.key("reviews", opts, self.item.shop)
        collection.register(self.store, self.sid, key, "reviews", opts)
        collection.finish(self.store, self.sid, key, error="previous timeout")
        self.service.errors[(self.sid, key)] = (time.time(), "previous timeout")

        async def collect(*args):
            cp.current.get().save()
            # Inspect this incomplete checkpoint before allowing another segment.
            self.service.queue_config['enabled'] = False
            raise cp.YieldCollection()

        with patch("backend.business.collect", new=collect):
            self.service.request_sync("reviews", opts, force=True)
            await self.drain()
        state = collection.state(self.store, self.sid, key)
        self.assertEqual(state["last_success"], 0)
        self.assertTrue(state["continuation"])
        self.assertEqual(state["failures"], 0)
        self.assertEqual(state["retry_at"], 0)
        self.assertEqual(state["last_error"], "")
        self.assertNotIn((self.sid, key), self.service.errors)

    async def test_backup_restores_and_keeps_history_and_ciphertext(self):
        maintenance = Maintenance(self.store)
        self.store.log_event("info", "test", "test", "test")
        self.store.write_cache(self.sid, "products:test", {"rows": [{"id": "a"}], "complete": True})
        result = await maintenance.backup()
        self.assertTrue(result["verified"])
        path = self.store.directory / "backups" / (result["name"] + ".sqlite3")
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            encrypted = db.execute("SELECT password FROM shops").fetchone()[0]
            self.assertNotEqual(encrypted, b"pw")
            self.assertEqual(self.store.cipher.decrypt(encrypted), b"pw")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM business_cache").fetchone()[0], 1)
        self.assertTrue(maintenance.listing()["retention_preview"]["automatic_deletion"])
        self.assertIsNotNone(self.store.read_cache(self.sid, "products:test"))

    async def test_manual_batched_read_finishes_while_automatic_collection_is_disabled(self):
        collection.save_settings(self.store, {"enabled": False, "batch_pages": 2})
        calls = []

        async def collect(*args):
            calls.append(1)

            async def fetch(page):
                ids = [str(page)] if page <= 3 else []
                return {"rows": [{"id": i} for i in ids], "ids": ids, "total": 3}

            return await cp.paginate("test", fetch, lambda *_: None)

        opts = options_for("reviews")
        with patch("backend.business.collect", new=collect):
            self.service.request_sync("reviews", opts, force=True)
            await asyncio.wait_for(self.drain(), 10)
        self.assertEqual(len(calls), 2)
        payload = self.store.read_cache(self.sid, self.service.key("reviews", opts, self.item.shop))["payload"]
        self.assertEqual(len(payload["rows"]), 3)
        self.assertTrue(payload["complete"])


class PolicyTests(unittest.TestCase):
    def test_load_pressure_reduces_then_recovers_within_configured_cap(self):
        load = LoadController()
        config = {**collection.DEFAULTS, "max_concurrent": 4, "realtime_concurrent": 3, "daily_concurrent": 1}
        self.assertEqual(load.update(config, now=1000, measurements=(90, 70))["max_concurrent"], 3)
        self.assertEqual(load.update(config, now=1001, measurements=(90, 70))["max_concurrent"], 3)
        self.assertEqual(load.update(config, now=1031, measurements=(90, 70))["max_concurrent"], 2)
        self.assertEqual(load.update(config, now=1032, measurements=(20, 60))["max_concurrent"], 2)
        self.assertEqual(load.update(config, now=1152, measurements=(20, 60))["max_concurrent"], 3)
        self.assertEqual(
            load.update({**config, "adaptive_enabled": False}, now=1153, measurements=(90, 99))["max_concurrent"], 4
        )

    def test_error_types_do_not_treat_uncertain_permission_as_denied(self):
        self.assertEqual(classify(LoginPending("验证码")), ("login", True))
        self.assertEqual(classify(ValueError("推广计划统计日期不一致")), ("adapter", True))
        self.assertEqual(classify(ValueError("请检查登录和查看权限")), ("transient", False))

    def test_moderate_browser_memory_does_not_ratchet_concurrency_to_two(self):
        load = LoadController()
        config = {**collection.DEFAULTS, "max_concurrent": 16, "realtime_concurrent": 14, "daily_concurrent": 2}
        for now in range(1000, 1500, 31):
            load.update(config, now=now, measurements=(40, 89))
        self.assertEqual(load.limit, 12)
        for now in range(1600, 2500, 31):
            load.update(config, now=now, measurements=(98, 98))
        self.assertEqual(load.limit, 2)

    def test_missing_campaign_report_stays_unknown_but_wrong_date_rejected(self):
        opts = {"date_from": "2026-10-09", "date_to": "2026-10-09"}
        row = campaign({"campaignId": 1, "reportInfoList": None}, opts)
        self.assertIsNone(row["ad_spend"])
        self.assertIn("未返回", row["note"])
        with self.assertRaises(ValueError):
            campaign(
                {
                    "campaignId": 1,
                    "reportInfoList": [{"condition": {"startTime": "2026-10-08", "endTime": "2026-10-08"}}],
                },
                opts,
            )
