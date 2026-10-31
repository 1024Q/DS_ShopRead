"""Owned idle-tab reclamation without changing active task concurrency."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from backend.read_page_pool import borrow, close_idle


class Reader:
    def __init__(self, session):
        self.session = session
        self.pending = {}
        self.body_tasks = set()
        self.evaluate = AsyncMock(return_value=True)
        self.call = AsyncMock()
        self.close = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


class PoolCapacityTests(unittest.IsolatedAsyncioTestCase):
    def root(self):
        return SimpleNamespace(shop={"id": 1, "platform": "pdd"}, state="logged_in", port=99)

    async def test_new_kind_closes_unrelated_idle_before_creating_target(self):
        root = self.root()
        created = []

        def factory(session):
            if created:
                created[0].close.assert_awaited_once()
            page = Reader(session)
            created.append(page)
            return page

        async with borrow(root, "overview", factory):
            pass
        async with borrow(root, "tickets", factory) as second:
            self.assertEqual(len(created), 2)
            self.assertFalse(root._idle_read_pages)
            second.close.assert_not_awaited()
        await close_idle(root)

    async def test_exact_kind_reuses_existing_page_without_new_target(self):
        root = self.root()
        factory = Mock(side_effect=Reader)
        async with borrow(root, "orders", factory) as first:
            pass
        async with borrow(root, "orders", factory) as second:
            self.assertIs(first, second)
            self.assertEqual(factory.call_count, 1)
            first.close.assert_not_awaited()
        await close_idle(root)

    async def test_parallel_reader_does_not_fence_other_active_reader(self):
        root = self.root()
        async with borrow(root, "overview", Reader) as first:
            async with borrow(root, "tickets", Reader) as second:
                self.assertIsNot(first, second)
                first.close.assert_not_awaited()
                second.close.assert_not_awaited()
            first.close.assert_not_awaited()
        first.close.assert_not_awaited()
        second.close.assert_not_awaited()
        self.assertEqual(len(root._idle_read_pages), 2)
        await close_idle(root)

    async def test_close_idle_attempts_every_page_and_clears_pool_before_awaiting(self):
        root = self.root()
        first = Reader(root)
        second = Reader(root)
        first.close.side_effect = OSError("already disconnected")
        timers = [Mock(), Mock()]
        root._idle_read_pages = {"a": (first, 0, None, timers[0]), "b": (second, 0, None, timers[1])}
        await close_idle(root)
        self.assertFalse(root._idle_read_pages)
        first.close.assert_awaited_once()
        second.close.assert_awaited_once()
        for timer in timers:
            timer.cancel.assert_called_once()
        await close_idle(root)
        first.close.assert_awaited_once()
        second.close.assert_awaited_once()

    async def test_cancelled_close_propagates_after_attempting_other_idle_pages(self):
        root = self.root()
        first = Reader(root)
        second = Reader(root)
        first.close.side_effect = asyncio.CancelledError()
        root._idle_read_pages = {"a": (first, 0, None, Mock()), "b": (second, 0, None, Mock())}
        with self.assertRaises(asyncio.CancelledError):
            await close_idle(root)
        first.close.assert_awaited_once()
        second.close.assert_awaited_once()
        self.assertFalse(root._idle_read_pages)

    async def test_stop_while_idle_cleanup_is_waiting_does_not_recache_active_reader(self):
        root = self.root()
        started = asyncio.Event()
        finish = asyncio.Event()
        idle = Reader(root)

        async def wait_close():
            started.set()
            await finish.wait()

        idle.close.side_effect = wait_close
        async with borrow(root, "orders", Reader) as active:
            root._idle_read_pages["idle"] = (idle, 0, None, Mock())
            closing = asyncio.create_task(close_idle(root))
            await started.wait()
            active.close.assert_not_awaited()
        active.close.assert_awaited_once()
        self.assertFalse(root._idle_read_pages)
        finish.set()
        await closing

    async def test_stop_during_target_creation_fences_later_return(self):
        root = self.root()
        started = asyncio.Event()
        finish = asyncio.Event()
        created = []

        class DelayedReader(Reader):
            async def __aenter__(self):
                self._pool_epoch = await close_idle(self.session)
                started.set()
                await finish.wait()
                return self

        async def run():
            async with borrow(root, "orders", DelayedReader) as page:
                created.append(page)

        reading = asyncio.create_task(run())
        await started.wait()
        await close_idle(root)
        finish.set()
        with self.assertRaisesRegex(ValueError, "浏览器状态已变化"):
            await reading
        self.assertFalse(created)
        self.assertFalse(root._idle_read_pages)


if __name__ == "__main__":
    unittest.main()
