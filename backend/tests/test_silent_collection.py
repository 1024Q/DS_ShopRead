"""Silent background collection, per-shop debug mode and queue de-duplication."""

import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.browser import Session
from backend.business import BusinessService
from backend.business_reader import foreground
from backend.collection_queue import DUPLICATE_STAGE, duplicate_step
from backend.scheduler import Scheduler
from backend.shop_lock import ShopLock
from backend.store import Store


class SilentSlotTests(unittest.IsolatedAsyncioTestCase):
    async def run_slot(self, debug_mode):
        service = BusinessService(None, None)
        item = SimpleNamespace(
            lock=ShopLock(), shop={"id": 1}, debug_mode=debug_mode, headless=False, quiet_window=AsyncMock()
        )
        async with service.slot((1, "overview"), item, "overview") as meta:
            stage = meta["stage"]
        return item, stage

    async def test_default_collection_never_shows_the_browser(self):
        item, stage = await self.run_slot(False)
        item.quiet_window.assert_awaited_once_with(False)
        self.assertEqual(stage, "后台静默采集")

    async def test_debug_mode_shows_the_task_browser(self):
        item, stage = await self.run_slot(True)
        item.quiet_window.assert_awaited_once_with(False)
        self.assertEqual(stage, "后台隐藏采集（调试日志）")

    async def test_reader_never_activates_window_in_either_mode(self):
        for debug in (False, True):
            page = SimpleNamespace(session=SimpleNamespace(debug_mode=debug), call=AsyncMock())
            # Connecting, borrowing and rebinding can all prepare the same page.
            for _ in range(3):
                await foreground(page)
            methods = [c.args[0] for c in page.call.await_args_list]
            self.assertNotIn("Page.bringToFront", methods)
            self.assertEqual(methods.count("Emulation.setFocusEmulationEnabled"), 3)
            self.assertEqual(methods.count("Page.setWebLifecycleState"), 3)


class DebugModeSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(self.folder.name)
        self.sid = self.store.add("debug-shop", "pw")
        self.session = Session(self.store.list()[0], self.store)
        self.session.window = AsyncMock()

    def tearDown(self):
        self.folder.cleanup()

    async def test_login_success_hides_unless_debug_mode(self):
        await self.session.auto_window(False)
        self.session.window.assert_awaited_once_with(False, activate=False)
        self.session.window.reset_mock()
        self.session.debug_mode = True
        await self.session.auto_window(False)
        self.session.window.assert_awaited_once_with(False, activate=False)

    async def test_verification_still_shows_window_outside_debug_mode(self):
        self.assertFalse(self.session.debug_mode)
        await self.session.auto_window(True)
        self.session.window.assert_awaited_once_with(False, activate=False)

    async def test_toggle_persists_and_applies_immediately(self):
        self.session.state = "logged_in"
        await self.session.set_debug_mode(True)
        # Automatic shows never steal keyboard focus.
        self.session.window.assert_awaited_once_with(False)
        self.assertTrue(self.store.debug_mode(self.sid))
        self.assertTrue(Session(self.store.list()[0], self.store).debug_mode)
        self.assertTrue(self.session.public()["debug_mode"])
        await self.session.set_debug_mode(False)
        self.session.window.assert_awaited_with(False)
        self.assertFalse(self.store.debug_mode(self.sid))
        with self.assertRaises(ValueError):
            await self.session.set_debug_mode("yes")

    async def test_toggle_on_stopped_browser_only_persists(self):
        self.session.state = "stopped"
        await self.session.set_debug_mode(True)
        self.session.window.assert_not_awaited()
        self.assertTrue(self.store.debug_mode(self.sid))

    def test_launch_visibility(self):
        self.assertFalse(self.session.launch_visibility())
        self.session.manual_visibility = True
        self.assertTrue(self.session.launch_visibility())
        self.session.manual_visibility, self.session.debug_mode = None, True
        self.assertFalse(self.session.launch_visibility())
        self.session.manual_visibility = False
        self.assertFalse(self.session.launch_visibility())


class PassiveShowTests(unittest.TestCase):
    def test_passive_show_does_not_activate(self):
        from backend.native_window import WindowController

        calls = []
        windows = [{"handle": 7, "visible": False, "minimized": False}]

        class Native:
            def identity(self, pid):
                return (1, "x")

            def windows(self, pid):
                return windows

            def change(self, handle, visible, activate=True):
                calls.append((handle, visible, activate))
                windows[0] = {**windows[0], "visible": visible}

        control = WindowController(9222, "profile", native=Native(), discover=lambda *a: 1)
        control.apply(True, None, False)
        self.assertEqual(calls, [(7, True, False)])


class DuplicateGenerationTests(unittest.IsolatedAsyncioTestCase):
    def test_same_sub_step_with_other_scope_is_a_duplicate(self):
        progress = {(1, "reviews:old"): {"kind": "reviews", "phase": "queued", "scope": {"stars": [5]}}}
        self.assertEqual(duplicate_step(progress, (1, "reviews:new"), "reviews", {"stars": [4]}), (1, "reviews:old"))
        # Its own job, another shop, another sub-step or a finished job is not.
        self.assertIsNone(duplicate_step(progress, (1, "reviews:old"), "reviews", {"stars": [5]}))
        self.assertIsNone(duplicate_step(progress, (2, "reviews:new"), "reviews", {}))
        self.assertIsNone(duplicate_step(progress, (1, "tickets:new"), "tickets", {}))
        self.assertIsNone(
            duplicate_step(progress, (1, "orders:new"), "orders", {"status": "待付款"}),
        )
        progress[(1, "reviews:old")]["phase"] = "done"
        self.assertIsNone(duplicate_step(progress, (1, "reviews:new"), "reviews", {}))

    async def test_repeated_due_plans_merge_and_forced_request_keeps_one_instance(self):
        from backend.tests.collection_fixture import policy
        service=BusinessService(None,None)
        config=policy(enabled=True);service.queue_config=config
        record={'id':1,'platform':'pdd'}
        item=SimpleNamespace(state='logged_in',retired=False,control_until=0)
        task={'failures':0,'retry_at':0,'last_success':0}
        prepared=({'stars':[]},'reviews:same',None,config,task)
        release=asyncio.Event()
        async def sync(*args):await release.wait()
        service.sync=sync
        try:
            with patch('backend.business.collection.mode',return_value='custom'):
                for _ in range(10):service.admit(record,item,'reviews',prepared,True,False,False)
                self.assertEqual(len(service.jobs),1)
                self.assertEqual(len(service.due_plans.entries),1)
                service.admit(record,item,'reviews',prepared,True,True,False)
                self.assertEqual(len(service.jobs),1)
            release.set();await asyncio.gather(*service.jobs.values())
        finally:await service.close()


if __name__ == "__main__":
    unittest.main()
