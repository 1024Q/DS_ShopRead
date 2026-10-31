import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from backend.window_layout import tile_rectangles, matches_process
from backend.browser import Session
from backend.store import Store
from backend import main


class LayoutTests(unittest.TestCase):
    def test_tiles_fill_work_area_without_overlap_for_many_counts(self):
        for area in [(0, 0, 1920, 1040), (0, 40, 1536, 824), (-1920, 0, 1920, 1040), (0, 0, 1080, 1880)]:
            for n in [1, 2, 3, 5, 7, 12, 17, 24, 50]:
                boxes = tile_rectangles(n, area)
                left, top, w, h = area
                self.assertEqual(len(boxes), n)
                self.assertEqual(sum(b[2] * b[3] for b in boxes), w * h)
                for i, (x, y, bw, bh) in enumerate(boxes):
                    self.assertTrue(x >= left and y >= top and x + bw <= left + w and y + bh <= top + h)
                    for xx, yy, ww, hh in boxes[i + 1 :]:
                        self.assertFalse(x < xx + ww and xx < x + bw and y < yy + hh and yy < y + bh)

    def test_process_requires_exact_port_and_profile(self):
        import os

        profile = os.path.abspath("profiles/one")
        for argument in [f"--user-data-dir={profile}", f'--user-data-dir="{profile}"', f'"--user-data-dir={profile}"']:
            self.assertTrue(
                matches_process(f"chrome {argument} --remote-debugging-port=9321 about:blank", 9321, profile)
            )
        self.assertFalse(
            matches_process(f"chrome --user-data-dir={profile}2 --remote-debugging-port=9321", 9321, profile)
        )
        self.assertFalse(
            matches_process(f"chrome --user-data-dir={profile} --remote-debugging-port=93210", 9321, profile)
        )


class BulkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.items = {}
        for i in range(3):
            sid = self.store.add("test" + str(i), "pw")
            item = Session(self.store.list()[-1], self.store)
            item.state = "logged_in" if i < 2 else "stopped"
            self.items[sid] = item
        self.client = TestClient(main.app)
        self.headers = {"Authorization": "Bearer " + main.TOKEN}
        self.patches = [
            patch.object(main, "store", self.store),
            patch.object(main, "session", side_effect=self.items.__getitem__),
            patch.object(main, "business", SimpleNamespace(cancel_shop=AsyncMock())),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.client.close()
        self.tmp.cleanup()

    def test_auth_validation_partial_results_and_stopped_skip(self):
        self.assertEqual(self.client.post("/api/shops/windows", json={"action": "show"}).status_code, 401)
        self.assertEqual(
            self.client.post("/api/shops/windows", headers=self.headers, json={"action": "close"}).status_code, 422
        )

        def operation(records, action):
            self.assertEqual(len(records), 2)
            self.assertEqual(action, "hide")
            return {
                "results": [
                    {"shop_id": 1, "ok": True, "windows": 1},
                    {"shop_id": 2, "ok": False, "error": "fixture failure", "windows": 0},
                ],
                "window_count": 1,
            }

        with patch("backend.window_layout.native_windows", side_effect=operation):
            r = self.client.post("/api/shops/windows", headers=self.headers, json={"action": "hide"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 1)
        self.assertFalse(r.json()["ok"])
        self.assertIs(self.items[1].manual_visibility, False)
        self.assertIsNone(self.items[2].manual_visibility)
        self.assertEqual(self.items[1].control_until, 0)

    def test_no_started_windows_returns_clear_error(self):
        for s in self.items.values():
            s.state = "stopped"
        self.assertEqual(
            self.client.post("/api/shops/windows", headers=self.headers, json={"action": "tile"}).status_code, 400
        )


class VisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_hide_prevents_verification_reshow_and_show_prevents_login_hide(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.add("one", "pw")
            item = Session(store.list()[0], store)
            item.window = AsyncMock()
            item.manual_visibility = False
            item.evaluate = AsyncMock(
                return_value={"logged": False, "verify": True, "failed": False, "signedOut": False}
            )
            await item.refresh()
            item.window.assert_not_awaited()
            item.manual_visibility = True
            item.evaluate = AsyncMock(
                return_value={"logged": True, "name": "", "verify": False, "failed": False, "signedOut": False}
            )
            await item.refresh()
            item.window.assert_not_awaited()
