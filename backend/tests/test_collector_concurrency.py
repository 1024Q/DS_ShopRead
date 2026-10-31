import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from backend.collector_concurrency import changes, request


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(
            max_concurrent=16,
            realtime_concurrent=8,
            daily_concurrent=6,
            scheduled_concurrent=2,
            shop_concurrent=2,
            borrow_idle=True,
        )

    def test_only_changed_fields_and_no_artificial_total_cap(self):
        self.assertEqual(changes({**self.config, "shop_concurrent": "3"}, self.config), {"shop_concurrent": 3})
        # Queue-specific quotas were removed; only node and shop limits are editable.
        self.assertEqual(
            changes({**self.config, "max_concurrent": "32", "realtime_concurrent": "24"}, self.config),
            {"max_concurrent": 32},
        )
        self.assertEqual(changes({**self.config, "shop_concurrent": "4"}, self.config), {"shop_concurrent": 4})
        self.assertEqual(changes({**self.config, "max_concurrent": "15"}, self.config), {"max_concurrent": 15})
        self.assertEqual(changes(self.config, self.config), {})

    def test_invalid_limits_rejected(self):
        for invalid in (
            {"shop_concurrent": 0},
            {"shop_concurrent": -1},
            {"max_concurrent": 0},
            {"max_concurrent": "2.5"},
        ):
            with self.assertRaises(ValueError):
                changes({**self.config, **invalid}, self.config)

    def test_save_uses_local_api_and_verifies_readback(self):
        original = httpx.Client
        calls = []

        def handler(req):
            calls.append((req.method, req.url.path, req.content))
            self.assertEqual(req.url.host, "127.0.0.1")
            return httpx.Response(200, json={"collection": {**self.config, "shop_concurrent": 3}})

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "api.key").write_text("test-key")
            with patch(
                "backend.collector_concurrency.httpx.Client",
                side_effect=lambda **kw: original(**kw, transport=httpx.MockTransport(handler)),
            ):
                self.assertEqual(request(tmp, 8001, {"shop_concurrent": 3})["shop_concurrent"], 3)
        self.assertEqual([c[:2] for c in calls], [("PUT", "/api/system/concurrency"), ("GET", "/api/system/settings")])
        self.assertEqual(calls[0][2], b'{"shop_concurrent":3}')

    def test_mismatched_readback_is_not_reported_as_success(self):
        original = httpx.Client
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "api.key").write_text("test-key")
            with patch(
                "backend.collector_concurrency.httpx.Client",
                side_effect=lambda **kw: original(
                    **kw, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"collection": self.config}))
                ),
            ):
                with self.assertRaises(ValueError):
                    request(tmp, 8001, {"shop_concurrent": 3})
