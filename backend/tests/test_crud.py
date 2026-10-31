import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from backend import main
from backend.store import Store
from backend.browser import Session


class CrudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store_patch = patch.object(main, "store", self.store)
        self.sessions_patch = patch.object(main, "sessions", {})
        self.stop_patch = patch.object(Session, "stop", new_callable=AsyncMock)
        self.store_patch.start()
        self.sessions_patch.start()
        self.stop = self.stop_patch.start()
        self.client = TestClient(main.app)
        self.headers = {"Authorization": "Bearer " + main.TOKEN}
        self.data = {"username": "sample", "password": "original-password", "name": "自定义名称", "browser": "system"}
        result = self.client.post("/api/shops", headers=self.headers, json=self.data)
        self.shop_id = result.json()["id"]
        self.url = f"/api/shops/{self.shop_id}"

    def tearDown(self):
        self.client.close()
        self.stop_patch.stop()
        self.sessions_patch.stop()
        self.store_patch.stop()
        self.temp.cleanup()

    def test_edit_blank_password_retains_credentials_and_session(self):
        old = main.session(self.shop_id)
        result = self.client.put(
            self.url, headers=self.headers, json={**self.data, "name": "新的显示名", "password": ""}
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["name"], "新的显示名")
        self.assertNotIn("password", result.json())
        self.assertEqual(self.store.credentials(self.shop_id)["password"], "original-password")
        self.assertIs(main.session(self.shop_id), old)
        self.stop.assert_not_awaited()

    def test_private_transfer_returns_saved_password_without_cache(self):
        result = self.client.post(self.url + "/transfer-credentials", headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {"password": "original-password"})
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(self.client.post(self.url + "/transfer-credentials").status_code, 401)
        self.stop.assert_not_awaited()

    def test_changed_credentials_retire_old_session_and_cookie_profile(self):
        old = main.session(self.shop_id)
        result = self.client.put(self.url, headers=self.headers, json={**self.data, "password": "new-password"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["state"], "stopped")
        self.assertTrue(old.retired)
        self.assertNotEqual(old.profile, main.session(self.shop_id).profile)
        self.assertEqual(self.store.credentials(self.shop_id)["password"], "new-password")
        self.assertNotIn(b"new-password", self.store.path.read_bytes())
        self.stop.assert_awaited_once()

    def test_duplicate_rejected_without_closing_browser(self):
        self.store.add("other", "pw")
        result = self.client.put(self.url, headers=self.headers, json={**self.data, "username": "OTHER"})
        self.assertEqual(result.status_code, 400)
        self.stop.assert_not_awaited()
        self.assertEqual(self.store.credentials(self.shop_id)["username"], "sample")

    def test_delete_removes_record_and_session_without_reusing_id(self):
        old = main.session(self.shop_id)
        result = self.client.delete(self.url, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.store.list(), [])
        self.assertNotIn(self.shop_id, main.sessions)
        self.assertTrue(old.retired)
        self.assertEqual(self.client.delete(self.url, headers=self.headers).status_code, 404)
        self.assertEqual(self.client.post(self.url + "/start", headers=self.headers).status_code, 404)
        next_id = self.store.add("next", "pw")
        self.assertGreater(next_id, self.shop_id)

    def test_failed_close_preserves_record_for_edit_and_delete(self):
        self.stop.side_effect = RuntimeError("browser busy")
        result = self.client.put(self.url, headers=self.headers, json={**self.data, "username": "new-account"})
        self.assertEqual(result.status_code, 503)
        result = self.client.delete(self.url, headers=self.headers)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(self.store.credentials(self.shop_id)["username"], "sample")
        self.assertFalse(main.session(self.shop_id).retired)

    def test_edit_delete_require_authentication(self):
        self.assertEqual(self.client.put(self.url, json=self.data).status_code, 401)
        self.assertEqual(self.client.delete(self.url).status_code, 401)


class AliasTests(unittest.IsolatedAsyncioTestCase):
    async def test_detected_name_does_not_overwrite_custom_name(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.add("sample", "pw", name="我的别名")
            item = Session(store.list()[0], store)
            item.evaluate = AsyncMock(return_value={"logged": True, "name": "平台店铺名称"})
            item.window = AsyncMock()
            await item.refresh()
            self.assertEqual(item.public()["name"], "我的别名")
            self.assertEqual(store.list()[0]["detected_name"], "平台店铺名称")
            item.retired = True
            with self.assertRaises(ValueError):
                await item.start()
