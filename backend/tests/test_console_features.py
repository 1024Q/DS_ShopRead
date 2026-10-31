import copy, tempfile, unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend import main
from backend.store import Store
from backend.browser import Session
from backend.platform_login import ENTRIES, allowed, home, validate_url
from backend.new_products import summarize
from backend.table_sort import sort_rows
from backend.business import create_router
from backend.remote_nodes import PATH


class ConsoleFeatures(unittest.TestCase):
    def test_sort_before_pagination_and_export(self):
        rows = [
            {
                "id": str(i),
                "shop_id": 1,
                "shop_name": "店铺",
                "stock": i,
                "created_at": "2026-01-01",
                "status": "在售中",
            }
            for i in range(45)
        ]
        payload = {"rows": rows, "metrics": [], "sources": []}
        service = SimpleNamespace(data=lambda *args: copy.deepcopy(payload))
        app = FastAPI()
        app.include_router(create_router(service, lambda: None))
        with TestClient(app) as c:
            result = c.get("/api/data/products?sort_by=stock&sort_direction=desc&page=2&page_size=20")
            self.assertEqual(result.status_code, 200)
            self.assertEqual([r["stock"] for r in result.json()["rows"]], list(range(24, 4, -1)))
            self.assertEqual(c.get("/api/data/products?sort_by=stock&sort_direction=invalid").status_code, 400)

    def test_missing_numbers_and_long_ids(self):
        rows = [
            {"id": "9007199254740993", "value": None},
            {"id": "9007199254740992", "value": 0},
            {"id": "9007199254740994", "value": 8},
        ]
        self.assertEqual([r["value"] for r in sort_rows(rows, "value", "desc")], [8, 0, None])
        self.assertEqual([r["value"] for r in sort_rows(rows, "value", "asc")], [0, 8, None])
        self.assertEqual(sort_rows(rows, "id", "asc")[0]["id"], "9007199254740992")

    def test_daily_year_rollover_monthly_dedup_missing(self):
        rows = [
            {"id": "1", "shop_id": 1, "created_at": "2026-01-01 00:01:00"},
            {"id": "2", "shop_id": 1, "created_at": "2025-12-31 23:59:00"},
            {"id": "1", "shop_id": 1, "created_at": "2026-01-01 00:01:00"},
        ]
        data = summarize(
            {"rows": rows, "sources": [{"shop_id": 1, "complete": True, "updated_at": "now"}, {"shop_id": 2}]},
            "2026-01-01",
            2025,
        )
        self.assertEqual((data["total"], data["previous_total"]), (1, 1))
        self.assertEqual(data["monthly"][11]["count"], 1)
        self.assertEqual(sum(m["count"] for m in data["monthly"]), 1)
        self.assertFalse(data["complete"])

    def test_all_platform_qr_and_host_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            for platform in ENTRIES:
                sid = store.add("", "", platform=platform, login_method="qr")
                record = store.list()[-1]
                self.assertTrue(Session(record, store).public()["adapter_available"])
                self.assertTrue(allowed(record, home(record)))
                self.assertFalse(allowed(record, "https://evil.com/?next=" + home(record)))
            self.assertFalse(allowed({"platform": "jd"}, "https://jd.com.evil.com/"))
            with self.assertRaises(ValueError):
                store.add("", "", platform="other", login_method="qr")
            self.assertTrue(
                store.add("", "", platform="other", login_method="qr", login_url="https://merchant.example.com/login")
            )
            with self.assertRaises(ValueError):
                validate_url("javascript:alert(1)")

    def test_atomic_operator_assign_and_rename_preserves_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            a = store.add("a", "pw")
            b = store.add("b", "pw")
            with (
                patch.object(main, "store", store),
                patch.object(main, "sessions", {}),
                patch.object(main.business, "cancel_shop", new_callable=AsyncMock) as cancel,
            ):
                c = TestClient(main.app)
                auth = {"Authorization": "Bearer " + main.TOKEN}
                self.assertEqual(
                    c.post("/api/shops/operators", json={"shop_ids": [a, b], "operator": "小陈"}).status_code, 401
                )
                self.assertEqual(
                    c.post(
                        "/api/shops/operators", headers=auth, json={"shop_ids": [a, b], "operator": "小陈"}
                    ).status_code,
                    200,
                )
                self.assertEqual(
                    c.post(
                        "/api/shops/operators", headers=auth, json={"shop_ids": [a, 99999], "operator": "小王"}
                    ).status_code,
                    400,
                )
                self.assertTrue(all(s["operator"] == "小陈" for s in store.list()))
                self.assertEqual(
                    c.post("/api/operators", headers=auth, json={"name": "陈运营", "previous": "小陈"}).status_code, 200
                )
                self.assertEqual(store.operators(), ["陈运营"])
                self.assertTrue(all(s["operator"] == "陈运营" for s in store.list()))
                self.assertTrue(all(s["profile_version"] == 0 for s in store.list()))
                cancel.assert_not_awaited()
                self.assertEqual(store.credentials(a)["password"], "pw")
                c.close()
            self.assertTrue(PATH.fullmatch("shops/operators"))
            self.assertTrue(PATH.fullmatch("operators"))


class PlatformFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_taofactory_bridge_never_marks_taobao_as_shop_logged_in(self):
        from backend import platform_login

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            store.add("", "", platform="taofactory", login_method="qr")
            s = Session(store.list()[0], store)
            s.state = "needs_verification"
            s.command = AsyncMock(return_value={})
            s.auto_window = AsyncMock()
            s.evaluate = AsyncMock(
                side_effect=[
                    {
                        "logged": True,
                        "name": "淘宝店铺",
                        "loginPage": False,
                        "signedOut": False,
                        "verify": False,
                        "failed": False,
                    },
                    "https://myseller.taobao.com/home",
                ]
            )
            result = await platform_login.refresh(s)
            self.assertFalse(result["logged"])
            self.assertFalse(s.authenticated)
            self.assertEqual(s.state, "needs_verification")
            self.assertEqual(store.list()[0]["detected_name"], "")
            s.command.assert_awaited_with("Page.navigate", {"url": home(s.shop)})

    async def test_other_platforms_never_use_pdd_password_filling(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            store.add("", "", platform="jd", login_method="qr")
            s = Session(store.list()[0], store)
            s.prepare_qr = AsyncMock(return_value=True)
            s.evaluate = AsyncMock()
            self.assertTrue(await s.fill_login())
            s.prepare_qr.assert_awaited_once()
            s.evaluate.assert_not_awaited()
