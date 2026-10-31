import io, tempfile, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.store import Store
from backend.browser import Session
from backend import main, tax_review
from backend.tax_info import options_for
from backend.tests.test_audit_store import row
from backend.workspace_api import create_router
from backend.new_products import summarize
from backend.xlsx_export import table, response
from backend.remote_nodes import PATH


class UpdatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("normal", "pw")

    def tearDown(self):
        self.tmp.cleanup()

    def test_operator_update_preserves_credentials_and_profile(self):
        self.store.update(self.sid, "normal", operator=" 张运营 ")
        restored = Store(self.tmp.name)
        record = restored.list()[0]
        self.assertEqual(record["operator"], "张运营")
        self.assertEqual(record["profile_version"], 0)
        self.assertEqual(restored.credentials(self.sid)["password"], "pw")
        self.assertNotIn("password", record)

    def test_qr_creation_and_password_mode_validation(self):
        with self.assertRaises(ValueError):
            self.store.add("", "")
        self.assertTrue(self.store.add("a", "", login_method="qr", platform="jd"))
        a = self.store.add("", "", login_method="qr")
        b = self.store.add("", "", login_method="qr")
        self.assertNotEqual(self.store.credentials(a)["username"], self.store.credentials(b)["username"])
        self.assertEqual(self.store.credentials(a)["password"], "")
        with self.assertRaises(ValueError):
            self.store.update(a, self.store.credentials(a)["username"], login_method="password")
        self.assertTrue(PATH.fullmatch(f"shops/{a}/qr-login"))

    def test_api_operator_edit_and_qr_auth(self):
        with patch.object(main, "store", self.store), patch.object(main, "sessions", {}):
            c = TestClient(main.app)
            headers = {"Authorization": "Bearer " + main.TOKEN}
            self.assertEqual(c.post("/api/shops", json={"login_method": "qr"}).status_code, 401)
            r = c.post("/api/shops", headers=headers, json={"login_method": "qr", "operator": "小陈"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["operator"], "小陈")
            self.assertNotIn("password", r.json())
            with patch.object(Session, "stop", new_callable=AsyncMock) as stop:
                r = c.put(f"/api/shops/{self.sid}", headers=headers, json={"username": "normal", "operator": "运营 A"})
                self.assertEqual(r.status_code, 200)
                stop.assert_not_awaited()
            c.close()

    def test_removed_manual_overwrite_cannot_modify_current_or_previous_tax_value(self):
        opts = options_for("2026-09")
        self.store.compare_tax(self.sid, {"rows": [row()]}, opts)
        self.store.compare_tax(self.sid, {"rows": [row(amount=200)]}, opts)
        before = tax_review.read(self.store, self.sid, "2026-09")
        app = FastAPI()
        app.include_router(create_router(SimpleNamespace(), self.store, lambda: None))
        with TestClient(app) as client:
            result = client.post(
                f"/api/tax-review/{self.sid}/2026-09",
                json={"revision": before["review"]["revision"], "overwrite": True},
            )
            self.assertEqual(result.status_code, 404)
        self.assertEqual(tax_review.read(self.store, self.sid, "2026-09"), before)
        self.assertEqual(before["baseline"]["tax_amount_cents"], 100)
        self.assertEqual(before["latest"]["tax_amount_cents"], 200)

    def test_missing_source_cannot_overwrite(self):
        opts = options_for("2026-09")
        self.store.compare_tax(self.sid, {"rows": [row()]}, opts)
        self.store.compare_tax(self.sid, {"rows": []}, opts)
        data = tax_review.read(self.store, self.sid, "2026-09")
        self.assertFalse(data["can_overwrite"])
        with self.assertRaises(ValueError):
            tax_review.save(self.store, self.sid, "2026-09", data["review"]["revision"], "", False, True)

    def test_new_goods_deduplicate_dates_and_preserve_unknown(self):
        data = {
            "sources": [
                {"shop_id": 1, "updated_at": "2026-10-10", "complete": True},
                {"shop_id": 2, "updated_at": None, "complete": False},
            ],
            "rows": [
                {"id": "a", "shop_id": 1, "created_at": "2026-10-10 00:00:00"},
                {"id": "a", "shop_id": 1, "created_at": "2026-10-10 00:00:00"},
                {"id": "b", "shop_id": 1, "created_at": "2026-10-09 23:59:59"},
                {"id": "c", "shop_id": 1, "created_at": ""},
            ],
        }
        d = summarize(data, "2026-10-10")
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["shops"][0]["unknown_dates"], 1)
        self.assertIsNone(d["shops"][1]["count"])
        self.assertFalse(d["complete"])

    def test_export_has_page_specific_chinese_headers_only(self):
        rows = [
            {
                "sales": 12,
                "order_rate": 2,
                "opening_balance": 100,
                "priority_reason": "关注",
                "unrecognized_internal_field": "secret",
                "operator": "A",
            }
        ]
        headers = table(rows, "products")[0]
        self.assertIn("累计销量", headers)
        self.assertIn("期初余额（元）", headers)
        self.assertNotIn("unrecognized_internal_field", headers)
        self.assertIn("成交金额", table(rows, "transactions")[0])
        result = response(
            "goodsdata", {"rows": rows, "sources": []}, {"统计范围": "week", "排序指标": "visitors", "排序方向": "desc"}
        )
        with ZipFile(io.BytesIO(result.body)) as z:
            text = z.read("xl/worksheets/sheet3.xml").decode()
            self.assertIn("商品数据", text)
            self.assertIn("近 7 日", text)
            self.assertIn("降序", text)
            self.assertNotIn("unrecognized_internal_field", z.read("xl/worksheets/sheet1.xml").decode())


class QrStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_qr_wait_does_not_submit_password_and_recognizes_success(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            store.add("", "", login_method="qr")
            s = Session(store.list()[0], store)
            s.auto_window = AsyncMock()
            s.evaluate = AsyncMock(return_value=True)
            self.assertTrue(await s.fill_login())
            self.assertEqual(s.state, "needs_verification")
            self.assertNotIn("VALUES", s.evaluate.call_args.args[0])
            s.evaluate = AsyncMock(
                return_value={
                    "logged": False,
                    "name": "",
                    "loginPage": True,
                    "signedOut": False,
                    "verify": False,
                    "failed": False,
                }
            )
            await s.refresh()
            self.assertEqual(s.state, "needs_verification")
            s.evaluate.return_value = {
                "logged": True,
                "name": "扫码店铺",
                "loginPage": False,
                "signedOut": False,
                "verify": False,
                "failed": False,
            }
            await s.refresh()
            self.assertEqual(store.list()[0]["name"], "扫码店铺")
            s.auto_window.assert_awaited_with(False)
