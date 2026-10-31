import asyncio, json, tempfile, time, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend import collection_checkpoint as cp
from backend.store import Store
from backend.read_page_pool import borrow, close_idle
from backend.task_timing import TaskMeta, observe_wait, fields
from backend.collection_queue import select_jobs
from backend import collection


class ProductTests(unittest.IsolatedAsyncioTestCase):
    async def test_pages_resume_and_all_rows_survive_yields(self):
        from backend.products import collect

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("test", "pw")
            calls = []

            def page():
                q = asyncio.Queue()
                q.put_nowait({"success": True, "result": {}})

                async def evaluate(script):
                    if "textContent" in script:
                        q.put_nowait(
                            {
                                "success": True,
                                "result": {"total": 3, "goods_list": [{"id": 1}]},
                                "_request": {"page": 1, "size": 1, "crawlerInfo": str(time.time())},
                            }
                        )
                        return True
                    n = int(script.split("p.onChange(", 1)[1].split(",", 1)[0])
                    calls.append(n)
                    q.put_nowait(
                        {
                            "success": True,
                            "result": {"total": 3, "goods_list": [{"id": n}]},
                            "_request": {"page": n, "size": 1},
                        }
                    )
                    return True

                return SimpleNamespace(products=q, evaluate=evaluate)

            result = None
            for _ in range(8):
                try:
                    async with cp.async_context(store, sid, "products:test", {"batch_pages": 2, "batch_seconds": 60}):
                        result = await collect(page())
                except cp.YieldCollection:
                    continue
                break
            self.assertIsNotNone(result)
            self.assertTrue(result["complete"])
            self.assertEqual({r["id"] for r in result["rows"]}, {"1", "2", "3"})
            self.assertNotIn(4, calls)
            with store.connect() as db:
                saved = db.execute("SELECT payload FROM collection_checkpoints").fetchone()[0]
            self.assertNotIn("crawlerInfo", saved)


class PoolTests(unittest.IsolatedAsyncioTestCase):
    def root(self):
        return SimpleNamespace(shop={"id": 1, "platform": "pdd"}, state="logged_in", port=99)

    def factory(self):
        page = SimpleNamespace(
            pending={}, body_tasks=set(), evaluate=AsyncMock(return_value=True), call=AsyncMock(), __aexit__=AsyncMock()
        )
        factory = lambda session: SimpleNamespace(__aenter__=AsyncMock(return_value=page))
        return page, factory

    async def test_reuse_is_exclusive_and_shop_local_and_exception_discards(self):
        root = self.root()
        page, factory = self.factory()
        async with borrow(root, "orders", factory) as first:
            self.assertIs(first, page)
        async with borrow(root, "orders", factory) as second:
            self.assertIs(second, page)
        page.__aexit__.assert_not_awaited()
        with self.assertRaises(ValueError):
            async with borrow(root, "orders", factory):
                raise ValueError("bad page")
        page.__aexit__.assert_awaited_once()
        self.assertEqual(root._idle_read_pages, {})
        other = self.root()
        otherpage, otherfactory = self.factory()
        async with borrow(other, "orders", otherfactory) as reader:
            self.assertIsNot(reader, page)
        await close_idle(other)

    async def test_cancel_and_identity_change_close_pages(self):
        root = self.root()
        page, factory = self.factory()
        with self.assertRaises(asyncio.CancelledError):
            async with borrow(root, "orders", factory):
                raise asyncio.CancelledError()
        page.__aexit__.assert_awaited_once()
        page2, factory2 = self.factory()
        async with borrow(root, "orders", factory2):
            root.shop["profile_version"] = 1
        page2.__aexit__.assert_awaited_once()

    async def test_idle_pages_expire(self):
        root = self.root()
        page, factory = self.factory()
        with patch("backend.read_page_pool.TTL", 0.01):
            async with borrow(root, "orders", factory):
                pass
            await asyncio.sleep(0.04)
        page.__aexit__.assert_awaited_once()
        self.assertFalse(root._idle_read_pages)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_error_does_not_block_healthy_shop(self):
        from backend.browser_recovery import request
        from backend.collection_dispatch import browser_blocked
        from backend.shop_lock import ShopLock

        item = SimpleNamespace(
            retired=False, wanted_running=True, lock=ShopLock(), state="logged_in", recover_home=AsyncMock()
        )
        lease = await item.lock.read()
        with (
            patch("backend.browser_recovery.health", AsyncMock(return_value="healthy")),
            patch("backend.browser_recovery.restored", AsyncMock()) as restored,
        ):
            await request(item, repair_page=True)
            self.assertFalse(browser_blocked(item))
            item.recover_home.assert_not_awaited()
            restored.assert_not_awaited()
            self.assertIn("按原重试计划", item.collection_recovery_result)
        lease.release()

    async def test_uncertain_probe_keeps_backoff_and_never_restarts(self):
        from backend.browser_recovery import request

        item = SimpleNamespace(retired=False, wanted_running=True)
        with (
            patch("backend.browser_recovery.health", AsyncMock(return_value="unknown")),
            patch("backend.browser_recovery.check", AsyncMock()) as check,
            patch("backend.browser_recovery.restored", AsyncMock()) as restored,
        ):
            await request(item)
            check.assert_not_awaited()
            restored.assert_not_awaited()


class TimingTests(unittest.TestCase):
    def test_stage_and_blocker_times_are_independent_of_queue_age(self):
        with patch("backend.task_timing.time.time", return_value=100):
            meta = TaskMeta(phase="queued", kind="orders", queued_at=1, stage="waiting")
        running = {(1, "first"): {"phase": "running", "kind": "orders", "scope": {}, "started_at": 99}}
        observe_wait((1, "next"), meta, running, collection.DEFAULTS, 100)
        self.assertEqual(fields(meta, 105)["current_wait_seconds"], 5)
        running[(1, "first")]["started_at"] = 105
        observe_wait((1, "next"), meta, running, collection.DEFAULTS, 105)
        self.assertEqual(fields(meta, 107)["current_wait_seconds"], 2)
        with patch("backend.task_timing.time.time", return_value=110):
            meta.update(phase="running", stage="read")
        self.assertEqual(fields(meta, 114)["stage_elapsed_seconds"], 4)
        self.assertEqual(meta["stage_durations"]["waiting"], 10)


class SharingTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_scope_reused_before_opening_page(self):
        from backend.request_reuse import complete
        from backend.business_reader import collect
        from backend.business import options_for

        root = SimpleNamespace(shop={"id": 1, "platform": "pdd"}, state="logged_in")
        opts = options_for("evidence")
        payload = {"complete": True, "rows": []}
        await complete(root, "evidence", opts, AsyncMock(return_value=payload))
        with patch("backend.business_reader.ReadPage", side_effect=AssertionError("must not open")):
            self.assertEqual(await collect(root, "evidence", opts), payload)

    def test_aftersales_query_tab_never_guessed_from_row_title(self):
        from backend.collection_demands import project
        from backend.business import options_for

        opts = options_for("aftersales", status="待商家处理")
        payload = {"complete": True, "rows": [{"id": "1", "status": "退款，待商家处理"}]}
        self.assertIsNone(project("aftersales", {**opts, "status": ""}, opts, payload))
        self.assertEqual(len(project("aftersales", opts, opts, payload)["rows"]), 1)
