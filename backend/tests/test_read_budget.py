import asyncio
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from contextlib import nullcontext
from backend.read_budget import ReadBudget, current
from backend.async_database import run
from backend import collection, collection_checkpoint as cp
from backend.store import Store
from backend.browser import Session
from backend.business import BusinessService, options_for


class ReadBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_wait_is_excluded_but_reading_still_times_out(self):
        meta = {}
        async with ReadBudget(0.05, meta):
            await run(time.sleep, 0.12)
            self.assertFalse(meta["timeout_paused"])
            await asyncio.sleep(0.005)
        with self.assertRaises(TimeoutError):
            async with ReadBudget(0.02, {}):
                await asyncio.sleep(0.2)

    async def test_external_cancellation_waits_for_database_then_propagates(self):
        started = asyncio.Event()
        done = []

        def write():
            time.sleep(0.06)
            done.append(True)

        async def work():
            async with ReadBudget(2, {}):
                started.set()
                await run(write)

        job = asyncio.create_task(work())
        await started.wait()
        await asyncio.sleep(0.01)
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertEqual(done, [True])


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("test", "pw")
        self.item = Session(self.store.list()[0], self.store)
        self.item.state = "logged_in"
        self.item.window = AsyncMock()  # Persistence tests do not launch a real browser window.
        self.service = BusinessService(self.store, lambda _: self.item)

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    async def test_slow_final_write_is_success_without_retry(self):
        opts = options_for("overview")
        key = self.service.key("overview", opts, self.item.shop)
        original = self.store.write_cache

        def slow_write(*args, **kwargs):
            time.sleep(0.15)
            return original(*args, **kwargs)

        with (
            patch("backend.business.collect", AsyncMock(return_value={"rows": [], "complete": True})),
            patch.object(self.store, "write_cache", side_effect=slow_write),
        ):
            self.service.request_sync("overview", opts)
            for g in self.service.queue_config["frequency_groups"]:
                g["timeout"] = 0.05
            await asyncio.gather(*self.service.jobs.values())
        state = collection.state(self.store, self.sid, key)
        self.assertGreater(state["last_success"], 0)
        self.assertEqual(state["failures"], 0)
        self.assertNotIn((self.sid, key), self.service.errors)
        self.assertTrue(self.store.read_cache(self.sid, key)["payload"]["complete"])

    async def test_failed_final_write_is_not_reported_as_success(self):
        import sqlite3

        opts = options_for("overview")
        key = self.service.key("overview", opts, self.item.shop)
        with (
            patch("backend.business.collect", AsyncMock(return_value={"rows": [], "complete": True})),
            patch.object(self.store, "write_cache", side_effect=sqlite3.OperationalError("database writer is busy")),
        ):
            self.service.request_sync("overview", opts)
            await asyncio.gather(*self.service.jobs.values())
        state = collection.state(self.store, self.sid, key)
        self.assertEqual(state["last_success"], 0)
        self.assertGreater(state["failures"], 0)
        self.assertIn((self.sid, key), self.service.errors)

    async def test_slow_page_timeout_preserves_previous_page(self):
        key = "reviews:slow"
        collection.register(self.store, self.sid, key, "reviews", options_for("reviews"))

        async def fetch(page):
            if page == 2:
                await asyncio.sleep(5)
            return {"rows": [{"id": str(page)}], "ids": [str(page)], "total": 3}

        with self.assertRaises(TimeoutError):
            async with ReadBudget(0.5, {}):
                async with cp.async_context(self.store, self.sid, key, {"batch_pages": 100, "batch_seconds": 60}):
                    await cp.paginate("part", fetch, lambda *_: None)
        with cp.context(self.store, self.sid, key, {}) as checkpoint:
            part = next(iter(checkpoint.data["parts"].values()))
            self.assertEqual(part["next"], 2)
            self.assertEqual(part["ids"], ["1"])

    async def test_near_deadline_saves_page_and_resumes_without_loss(self):
        key = "reviews:deadline"
        collection.register(self.store, self.sid, key, "reviews", options_for("reviews"))
        calls = []

        async def fetch(page):
            calls.append(page)
            return {"rows": [{"id": str(page)}], "ids": [str(page)], "total": 3}

        budget = SimpleNamespace(seconds=10, remaining=lambda: 10 if not calls else 0.01, database=nullcontext)
        token = current.set(budget)
        try:
            async with cp.async_context(self.store, self.sid, key, {"batch_pages": 100, "batch_seconds": 60}):
                with self.assertRaises(cp.YieldCollection):
                    await cp.paginate("part", fetch, lambda *_: None)
        finally:
            current.reset(token)
        self.assertEqual(calls, [1])
        self.assertTrue(collection.state(self.store, self.sid, key)["continuation"])
        async with cp.async_context(self.store, self.sid, key, {"batch_pages": 100, "batch_seconds": 60}):
            result = await cp.paginate("part", fetch, lambda *_: None)
        self.assertEqual({r["id"] for r in result["rows"]}, {"1", "2", "3"})
        self.assertTrue(result["complete"])
