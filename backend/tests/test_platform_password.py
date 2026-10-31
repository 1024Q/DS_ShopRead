import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from backend.store import Store
from backend.browser import Session
from backend import platform_login, platform_password


class PasswordFlows(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.store.add("test-account", "test-password", platform="taobao")
        self.session = Session(self.store.list()[0], self.store)
        self.session.auto_window = AsyncMock()
        self.session.command = AsyncMock()

    async def test_non_pdd_password_routes_to_password_adapter(self):
        with patch.object(platform_password, "prepare", new_callable=AsyncMock, return_value=True) as adapter:
            self.assertTrue(await self.session.fill_login())
            adapter.assert_awaited_once_with(self.session)

    async def test_qr_override_never_reads_password(self):
        self.session.qr_requested = True
        self.session.prepare_qr = AsyncMock(return_value=True)
        with patch.object(self.store, "credentials") as credentials:
            await self.session.fill_login()
            credentials.assert_not_called()

    async def test_logged_in_never_fills_or_submits(self):
        with (
            patch.object(platform_login, "refresh", new_callable=AsyncMock, return_value={"logged": True}),
            patch.object(platform_password, "evaluate_login", new_callable=AsyncMock) as evaluate,
        ):
            self.assertTrue(await platform_password.prepare(self.session))
            evaluate.assert_not_awaited()

    async def test_switch_then_submit_once(self):
        with (
            patch.object(platform_login, "refresh", new_callable=AsyncMock, return_value={"logged": False}),
            patch.object(
                platform_password,
                "evaluate_login",
                new_callable=AsyncMock,
                side_effect=[{"switched": True}, {"submitted": True}],
            ) as evaluate,
            patch.object(platform_password.asyncio, "sleep", new_callable=AsyncMock),
        ):
            self.assertTrue(await platform_password.prepare(self.session))
            self.assertEqual(evaluate.await_count, 2)
            self.assertEqual(self.session.state, "logging_in")
            self.assertTrue(self.session.auto_login_attempted)
            self.assertNotIn("test-password", self.session.recovery_status)

    async def test_challenge_pauses_after_filling(self):
        with (
            patch.object(platform_login, "refresh", new_callable=AsyncMock, return_value={"logged": False}),
            patch.object(
                platform_password,
                "evaluate_login",
                new_callable=AsyncMock,
                return_value={"filled": True, "challenge": True},
            ) as evaluate,
        ):
            await platform_password.prepare(self.session)
            evaluate.assert_awaited_once()
            self.assertEqual(self.session.state, "needs_verification")

    async def test_missing_entry_reports_manual_action_without_fake_success(self):
        with (
            patch.object(platform_login, "refresh", new_callable=AsyncMock, return_value={"logged": False}),
            patch.object(platform_password, "evaluate_login", new_callable=AsyncMock, return_value={"missing": True}),
            patch.object(platform_password.asyncio, "sleep", new_callable=AsyncMock),
        ):
            self.assertFalse(await platform_password.prepare(self.session))
            self.assertEqual(self.session.state, "needs_verification")
            self.assertIn("二维码登录", self.session.recovery_status)
            self.session.command.assert_awaited_once_with(
                "Page.navigate", {"url": platform_login.home(self.session.shop)}
            )

    async def test_pending_security_challenge_never_fills_or_navigates(self):
        with (
            patch.object(
                platform_login, "refresh", new_callable=AsyncMock, return_value={"logged": False, "verify": True}
            ),
            patch.object(platform_password, "evaluate_login", new_callable=AsyncMock) as evaluate,
        ):
            self.assertFalse(await platform_password.prepare(self.session))
            evaluate.assert_not_awaited()
            self.session.command.assert_not_awaited()

    async def test_detected_name_updates_without_overwriting_alias(self):
        self.session.shop["alias"] = "我的淘宝店"
        self.session.evaluate = AsyncMock(return_value={"logged": True, "name": "蓝怡宝旗舰店"})
        await platform_login.refresh(self.session)
        self.assertEqual(self.session.shop["name"], "我的淘宝店")
        self.assertEqual(self.store.list()[0]["detected_name"], "蓝怡宝旗舰店")

    async def test_password_error_marks_failed(self):
        self.session.evaluate = AsyncMock(return_value={"logged": False, "failed": True})
        await platform_login.refresh(self.session)
        self.assertEqual(self.session.state, "login_failed")
