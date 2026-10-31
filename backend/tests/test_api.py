import asyncio
import unittest
import tempfile
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from backend.main import app, TOKEN
from backend.store import Store


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.logs_store = Store(self.temp.name)
        self.store_patch = patch("backend.main.store", self.logs_store)
        self.store_patch.start()
        self.client = TestClient(app)
        self.headers = {"Authorization": "Bearer " + TOKEN}

    def tearDown(self):
        self.store_patch.stop()
        self.temp.cleanup()

    def test_log_endpoint_requires_auth_and_records_client_errors(self):
        self.assertEqual(self.client.get("/api/logs").status_code, 401)
        result = self.client.post("/api/logs", headers=self.headers, json={"message": "界面错误 token=private"})
        self.assertEqual(result.status_code, 200)
        logs = self.client.get("/api/logs?level=error", headers=self.headers).json()["rows"]
        self.assertEqual(len(logs), 1)
        self.assertNotIn("private", logs[0]["message"])
        self.assertEqual(self.client.get("/api/logs?limit=500", headers=self.headers).status_code, 422)

    def test_authentication(self):
        self.assertEqual(self.client.get("/api/shops").status_code, 401)
        result = self.client.get("/api/shops", headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(all("password" not in shop for shop in result.json()))

    def test_input_route(self):
        mock = AsyncMock()

        class Fake:
            lock = asyncio.Lock()
            command = mock

        with patch("backend.main.session", return_value=Fake()):
            result = self.client.post(
                "/api/shops/1/input", headers=self.headers, json={"type": "text", "text": "123456"}
            )
        self.assertEqual(result.status_code, 200)
        mock.assert_awaited_once_with("Input.insertText", {"text": "123456"})

    def test_invalid_input(self):
        result = self.client.post("/api/shops/1/input", headers=self.headers, json={"type": "mousePressed", "x": -1})
        self.assertEqual(result.status_code, 422)
