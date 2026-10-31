import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from backend.browser import Session, HOME
from backend.store import Store
from backend.business import BusinessService, options_for
from backend.read_errors import PageReadError, LoginPending
from backend.tests.collection_fixture import assign, drain


def snapshot(**values):
    return dict(logged=False, signedOut=False, verify=False, failed=False, name="", **values)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.id = self.store.add("merchant", "pw")
        self.session = Session(self.store.list()[0], self.store)
        self.session.state = "logged_in"
        self.session.authenticated = True
        self.session.command = AsyncMock()
        self.session.window = AsyncMock()
        self.session.fill_login = AsyncMock(return_value=True)
        self.sleep = patch("backend.browser.asyncio.sleep", new=AsyncMock())
        self.sleep.start()
        assign(self.store, {"tax@base": "custom"})

    async def asyncTearDown(self):
        self.sleep.stop()
        self.directory.cleanup()

    async def test_child_page_does_not_trigger_password_submission(self):
        self.session.evaluate = AsyncMock(side_effect=[snapshot(), {**snapshot(), "logged": True, "name": "店铺"}])
        self.assertTrue(await self.session.recover_home())
        self.session.command.assert_awaited_once_with("Page.navigate", {"url": HOME + "home/"})
        self.session.fill_login.assert_not_awaited()

    async def test_confirmed_signed_out_submits_once_then_hides_on_success(self):
        self.session.evaluate = AsyncMock(
            side_effect=[{**snapshot(), "signedOut": True}, {**snapshot(), "logged": True, "name": "店铺"}]
        )
        self.assertTrue(await self.session.recover_home())
        self.session.fill_login.assert_awaited_once()
        self.assertEqual([call.args[0] for call in self.session.window.await_args_list], [False, False])

    async def test_verification_is_preserved_without_reloading_or_resubmitting(self):
        self.session.evaluate = AsyncMock(
            side_effect=[{**snapshot(), "signedOut": True}, {**snapshot(), "verify": True}]
        )
        self.assertFalse(await self.session.recover_home())
        self.assertEqual(self.session.state, "needs_verification")
        self.assertFalse(await self.session.recover_home())
        self.session.command.assert_awaited_once()
        self.session.fill_login.assert_awaited_once()

    async def test_bad_password_stops_automatic_attempts(self):
        self.session.evaluate = AsyncMock(
            side_effect=[{**snapshot(), "signedOut": True}, {**snapshot(), "failed": True}]
        )
        self.assertFalse(await self.session.recover_home())
        self.assertEqual(self.session.state, "login_failed")
        self.assertFalse(await self.session.recover_home())
        self.session.fill_login.assert_awaited_once()

    async def test_qr_login_page_can_switch_to_saved_account_login(self):
        self.session.evaluate = AsyncMock(
            side_effect=[{**snapshot(), "loginPage": True}, {**snapshot(), "verify": True}]
        )
        self.assertFalse(await self.session.recover_home())
        self.session.fill_login.assert_awaited_once()
        self.assertEqual(self.session.state, "needs_verification")

    async def test_read_failure_preserves_backoff_and_requests_separate_health_check(self):
        from backend import collection

        service = BusinessService(self.store, lambda _: self.session)
        opts = options_for("overview")
        key = service.key("overview", opts, self.session.shop)
        self.session.recover_home = AsyncMock(return_value=True)
        with (
            patch("backend.business.collect", new=AsyncMock(side_effect=PageReadError("页面读取失败"))) as read,
            patch("backend.browser_recovery.request", return_value=None) as recover,
        ):
            service.request_sync("overview", opts, force=True)
            await drain(service)
            read.assert_awaited_once()
            recover.assert_called_once()
        self.session.recover_home.assert_not_awaited()
        self.assertGreater(collection.state(self.store, self.id, key)["retry_at"], 0)
        self.assertIsNone(self.store.read_cache(self.id, key))
        await service.close()

    async def test_current_month_amount_updates_do_not_emit_difference_warning(self):
        from backend.tests.test_audit_store import row
        from backend.tax_info import options_for as tax_options
        from datetime import datetime, timezone

        now = datetime(2026, 9, 20, tzinfo=timezone.utc).timestamp()
        opts = tax_options("2026-09")
        service = BusinessService(self.store, lambda _: self.session)
        key = service.key("tax", opts, self.session.shop)
        with patch("backend.audit_store.time.time", return_value=now):
            self.store.compare_tax(self.id, {"rows": [row()]}, opts)
            with patch(
                "backend.business.collect", new=AsyncMock(return_value={"rows": [row(amount=200)], "complete": True})
            ):
                service.request_sync("tax", opts, force=True)
                await drain(service)
        self.assertIsNotNone(self.store.read_cache(self.id, key))
        self.assertFalse(any(log["event"] == "database_difference" for log in self.store.read_logs()))

    async def test_validation_error_keeps_saved_record_without_relogin(self):
        service = BusinessService(self.store, lambda _: self.session)
        opts = options_for("overview")
        key = service.key("overview", opts, self.session.shop)
        saved = {"rows": [], "complete": True}
        self.store.write_cache(self.id, key, saved)
        self.session.recover_home = AsyncMock()
        with patch("backend.business.collect", new=AsyncMock(side_effect=ValueError("金额或笔数不一致"))):
            service.request_sync("overview", opts, force=True)
            await drain(service)
        self.session.recover_home.assert_not_awaited()
        self.assertEqual(self.store.read_cache(self.id, key)["payload"], saved)

    async def test_waiting_verification_resumes_even_with_fresh_cache_and_auto_off(self):
        service = BusinessService(self.store, lambda _: self.session)
        opts = options_for("overview")
        key = service.key("overview", opts, self.session.shop)
        self.store.write_cache(self.id, key, {"rows": [], "complete": True})

        with self.store.connect() as db:
            db.execute("UPDATE business_cache SET updated=updated-11")

        async def pending():
            self.session.state = "needs_verification"
            return False

        self.session.recover_home = AsyncMock(side_effect=pending)
        with patch("backend.business.collect", new=AsyncMock(side_effect=LoginPending("登录失效"))):
            service.request_sync("overview", opts, force=True)
            await drain(service)
        self.assertIn((self.id, key), service.waiting_login)
        self.assertFalse(service.data("overview", opts)["sources"][0]["syncing"])
        self.session.state = "logged_in"
        import time
        from backend.collection_recovery import browser_restored

        browser_restored(self.store, self.id, time.time())
        with patch("backend.business.collect", new=AsyncMock(return_value={"rows": [], "complete": True})):
            service.request_sync("overview", opts)
            data = service.data("overview", opts, refresh=False)
            self.assertTrue(data["sources"][0]["syncing"])
            await service.jobs[(self.id, key)]
        self.assertNotIn((self.id, key), service.waiting_login)

    async def test_financial_details_survive_cache_cleanup_and_store_restart(self):
        payload = {"rows": [{"breakdown": {"income": [{"label": "交易收入", "count": 12, "amount_cents": 123}]}}]}
        self.store.write_cache(self.id, "reconciliation_day:test", payload)
        for n in range(35):
            self.store.write_cache(self.id, f"reviews:{n}", {"rows": []})
        reopened = Store(self.directory.name)
        self.assertEqual(reopened.read_cache(self.id, "reconciliation_day:test")["payload"], payload)
