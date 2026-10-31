import asyncio
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend import collection, collection_checkpoint as cp
from backend.business import BusinessService, options_for
from backend.business_reader import BEIJING
from backend.collection_queue import select_jobs
from backend.demand_refresh import timing, order
from backend.incremental_collection import merge_delta
from backend.shop_lock import ShopLock
from backend.store import Store


class SchedulingTests(unittest.TestCase):
    def test_success_deadline_does_not_add_another_interval_to_queue_wait(self):
        cfg = {**collection.DEFAULTS, "platform_modes": {"pdd": {"orders": "realtime"}}}
        state = {"queued_at": 190, "last_success": 100}
        self.assertEqual(timing("orders", {}, state, cfg, "pdd", 200)["queue_deadline"], 160)
        resumed = {**state, "continuation": True, "queue_turn_at": 195}
        self.assertEqual(order("orders", {}, resumed, cfg, "pdd", 200)[0], 1)

    def test_same_overdue_band_rotates_shops_and_preserves_limits(self):
        cfg = {
            **collection.DEFAULTS,
            "frequency_version": 1,
            "frequency_groups": [{"id": "realtime", "name": "实时", "seconds": 60, "timeout": 60}],
            "task_modes": {"pdd": {"overview": "realtime", "products": "realtime"}},
            "max_concurrent": 2,
            "shop_concurrent": 3,
        }

        def meta(kind):
            return {"kind": kind, "scope": {}, "phase": "queued", "queued_at": 10}

        p = {(1, "overview"): meta("overview"), (1, "products"): meta("products"), (2, "overview"): meta("overview")}
        chosen = select_jobs(p, cfg, now=200)
        self.assertEqual({k[0] for k in chosen}, {1, 2})
        p[(1, "overview")]["phase"] = "running"
        chosen = select_jobs(p, cfg, now=200)
        self.assertEqual(chosen, [(2, "overview")])

    def test_loaded_process_version_is_not_disk_version_after_replacement(self):
        from backend.process_status import capture

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "collector-version.json").write_text('{"version":"old"}')
            loaded = capture(root)
            (root / "collector-version.json").write_text('{"version":"new"}')
            self.assertEqual(loaded["version"], "old")
            self.assertEqual(capture(root)["version"], "new")
            self.assertTrue(loaded["pid"] and loaded["started_at"] and loaded["boot_id"])

    def test_shared_success_never_uses_a_slot_but_manual_refresh_still_runs(self):
        cfg = {**collection.DEFAULTS, "max_concurrent": 1}
        meta = {"kind": "overview", "scope": {}, "phase": "queued", "queued_at": 1, "shared_result_ready": True}
        self.assertEqual(select_jobs({(1, "one"): meta}, cfg), [])
        self.assertEqual(select_jobs({(1, "one"): {**meta, "force": True}}, cfg), [(1, "one")])


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_browser_does_not_clear_failure_backoff(self):
        from backend.browser_recovery import request

        item = SimpleNamespace(retired=False, wanted_running=True, lock=ShopLock(), state="logged_in")
        with (
            patch("backend.browser_recovery.check", new=AsyncMock(return_value=None)),
            patch("backend.browser_recovery.restored", new=AsyncMock()) as restored,
        ):
            await request(item)
            restored.assert_not_awaited()

    async def test_many_module_failures_share_one_recovery_and_wake_scheduler(self):
        from backend.browser_recovery import request
        from backend.collection_dispatch import browser_blocked

        release = asyncio.Event()
        started = asyncio.Event()

        async def repair():
            started.set()
            await release.wait()
            return True

        item = SimpleNamespace(
            retired=False,
            wanted_running=True,
            lock=ShopLock(),
            state="logged_in",
            recover_home=AsyncMock(side_effect=repair),
        )
        wake = asyncio.Event()
        with (
            patch("backend.browser_recovery.health", new=AsyncMock(return_value="login")),
            patch("backend.browser_recovery.restored", new=AsyncMock()) as restored,
        ):
            task = request(item, wake, repair_page=True)
            await started.wait()
            self.assertIs(request(item, wake, repair_page=True), task)
            self.assertTrue(browser_blocked(item))
            release.set()
            await task
            item.recover_home.assert_awaited_once()
            restored.assert_awaited_once()
            self.assertTrue(wake.is_set())
            self.assertFalse(browser_blocked(item))
            self.assertIsNone(request(item, wake, repair_page=True))


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_page_subqueries_yield_and_resume_completed_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            sid = store.add("test", "pw")
            calls = []

            async def fetch(n):
                calls.append(n)
                return {"rows": [{"id": "1"}], "ids": ["1"], "total": 1}

            async with cp.async_context(store, sid, "test", {"batch_pages": 1, "batch_seconds": 60}):
                await cp.paginate("one", fetch, lambda *_: None)
                with self.assertRaises(cp.YieldCollection):
                    await cp.paginate("two", fetch, lambda *_: None)
            async with cp.async_context(store, sid, "test", {"batch_pages": 1, "batch_seconds": 60}):
                await cp.paginate("one", fetch, lambda *_: None)
                result = await cp.paginate("two", fetch, lambda *_: None)
            self.assertEqual(calls, [1, 1])
            self.assertTrue(result["complete"])

    async def test_open_status_query_uses_known_old_pending_window(self):
        opts = options_for("tickets")
        stamp = opts["start"] + 86400
        row = {
            "id": "old",
            "status": "待处理",
            "created_at": datetime.fromtimestamp(stamp, BEIJING).strftime("%Y-%m-%d %H:%M:%S"),
        }
        calls = []

        async def read(o):
            calls.append(o)
            return {"rows": [row] if o.get("status") else [], "complete": True}

        result = await merge_delta("tickets", opts, {"rows": [row], "_sync": {"full_at": 1}}, opts["end"] - 3600, read)
        self.assertEqual(len(calls), 2)
        self.assertEqual((calls[1]["start"], calls[1]["end"]), (stamp - 3600, stamp + 3600))
        self.assertEqual(result["rows"], [row])

    async def test_unknown_creation_time_keeps_full_status_query(self):
        opts = options_for("tickets")
        row = {"id": "old", "status": "待处理"}
        calls = []

        async def read(o):
            calls.append(o)
            return {"rows": [row] if o.get("status") else [], "complete": True}

        await merge_delta("tickets", opts, {"rows": [row], "_sync": {"full_at": 1}}, opts["end"] - 3600, read)
        self.assertEqual((calls[1]["start"], calls[1]["end"]), (opts["start"], opts["end"]))

    async def test_no_open_records_needs_only_the_new_record_query(self):
        read = AsyncMock(return_value={"rows": [], "complete": True})
        opts = options_for("tickets")
        await merge_delta("tickets", opts, {"rows": [], "_sync": {"full_at": 1}}, opts["end"] - 3600, read)
        read.assert_awaited_once()
