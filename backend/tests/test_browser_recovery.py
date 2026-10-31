import unittest
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from backend.browser_recovery import check
from backend.collection_errors import describe
from backend.shop_lock import ShopLock


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def item(self, **kw):
        data = dict(
            retired=False,
            wanted_running=True,
            lock=ShopLock(),
            state="logged_in",
            shop={"id": 1},
            store=MagicMock(),
            refresh=AsyncMock(return_value={}),
            target=AsyncMock(),
            close_cdp=AsyncMock(),
            recover_home=AsyncMock(),
            start=AsyncMock(),
            auto_login_attempted=False,
            last_recovery=0,
            error="old",
        )
        data.update(kw)
        return SimpleNamespace(**data)

    async def test_manual_stop_retired_control_and_update_are_not_restarted(self):
        for kw in (
            {"wanted_running": False, "state": "stopped"},
            {"retired": True},
            {"control_until": time.time() + 100},
        ):
            item = self.item(**kw)
            await check(item)
            item.start.assert_not_awaited()
        item = self.item(state="stopped")
        with patch("backend.browser_recovery.update_gate.active", return_value=True):
            await check(item)
        item.start.assert_not_awaited()

    async def test_disconnected_browser_restarts_once_then_cools_down(self):
        item = self.item(
            refresh=AsyncMock(side_effect=ConnectionError()), target=AsyncMock(side_effect=ConnectionError())
        )
        await check(item)
        item.start.assert_awaited_once_with(automatic=True)
        await check(item)
        self.assertEqual(item.start.await_count, 1)

    async def test_live_browser_reconnects_without_restart(self):
        item = self.item(refresh=AsyncMock(side_effect=TimeoutError()))
        await check(item)
        item.recover_home.assert_awaited_once()
        item.start.assert_not_awaited()

    async def test_wanted_stopped_restarts_and_failed_restart_backs_off(self):
        item = self.item(state="stopped", start=AsyncMock(side_effect=TimeoutError()))
        await check(item)
        self.assertIn("30 秒", item.error)
        self.assertGreater(item.restart_after, time.monotonic())

    async def test_busy_shop_not_interrupted(self):
        item = self.item(state="stopped")
        async with item.lock:
            await check(item)
        item.start.assert_not_awaited()

    def test_running_intent_persists_and_stop_overrides_it(self):
        import tempfile
        from backend.store import Store
        from backend.browser_recovery import remember, wanted

        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            item = self.item(store=store)
            self.assertIsNone(wanted(store, 1))
            remember(item, True)
            self.assertTrue(wanted(Store(directory), 1))
            remember(item, False)
            self.assertFalse(wanted(Store(directory), 1))

    def test_error_description_no_raw_exception_secrets(self):
        self.assertIn("超时", describe(TimeoutError()))
        self.assertIn("连接中断", describe(ConnectionError("secret")))
        self.assertNotIn("secret", describe(RuntimeError("secret")))


if __name__ == "__main__":
    unittest.main()
