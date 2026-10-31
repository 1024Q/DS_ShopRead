import tempfile
import unittest
from unittest.mock import AsyncMock
from backend.store import Store
from backend.browser import Session


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.add("merchant", "pw")
        self.session = Session(self.store.list()[0], self.store)
        self.session.window = AsyncMock()

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_login_identifies_and_hides_once(self):
        self.session.evaluate = AsyncMock(
            return_value={"logged": True, "name": "真实店铺", "verify": False, "failed": False, "signedOut": False}
        )
        await self.session.refresh()
        self.assertEqual(self.store.list()[0]["name"], "真实店铺")
        self.assertEqual(self.session.state, "logged_in")
        self.session.window.assert_awaited_once_with(False, activate=False)
        await self.session.refresh()
        self.assertEqual(self.session.window.await_count, 1)

    async def test_verification_shows_browser(self):
        self.session.evaluate = AsyncMock(
            return_value={"logged": False, "name": "", "verify": True, "failed": False, "signedOut": False}
        )
        await self.session.refresh()
        self.assertEqual(self.session.state, "needs_verification")
        self.session.window.assert_awaited_once_with(False, activate=False)

    async def test_restart_does_not_claim_hidden_window_visible(self):
        self.session.authenticated = True
        self.session.target = AsyncMock(return_value={})
        self.session.evaluate = AsyncMock(
            return_value={"logged": True, "name": "真实店铺", "verify": False, "failed": False, "signedOut": False}
        )
        await self.session.start()
        self.assertFalse(self.session.visible)
        self.session.window.assert_not_awaited()

    async def test_browser_kinds_keep_separate_profiles(self):
        alternate = Session({**self.store.list()[0], "browser": "bundled"}, self.store)
        self.assertNotEqual(alternate.profile, self.session.profile)
