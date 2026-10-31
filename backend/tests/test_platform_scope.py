import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.business import create_router


class PlatformScopeTests(unittest.TestCase):
    def test_non_pdd_requests_never_return_pdd_data_or_enqueue(self):
        service = SimpleNamespace(data=Mock())
        app = FastAPI()
        app.include_router(create_router(service, lambda: None))
        with TestClient(app) as client:
            for platform in ("jd", "douyin", "taofactory", "xiaohongshu", "wechat", "other", "unknown"):
                response = client.get("/api/data/overview", params={"platform": platform, "refresh": "true"})
                self.assertEqual(response.status_code, 400)
                self.assertIn("尚未接入", response.json()["detail"])
        service.data.assert_not_called()
