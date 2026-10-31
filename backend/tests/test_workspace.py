import asyncio
import io
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile
from xml.etree import ElementTree as ET
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from backend.store import Store
from backend.business import BusinessService, create_router, options_for
from backend.workspace_api import create_router as workspace_router, product_snapshot
from backend.tax_info import options_for as tax_options, group_rows
from backend import tax_review
from backend.xlsx_export import workbook, response
from backend.tests.test_audit_store import row


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("test", "pw", name="店一")

    def tearDown(self):
        self.temp.cleanup()

    def test_tax_version_reviews_preserve_original_and_reject_stale(self):
        opts = tax_options("2026-09")
        self.store.compare_tax(self.sid, {"rows": [row()]}, opts)
        self.store.compare_tax(self.sid, {"rows": [row(amount=200)]}, opts)
        first = tax_review.read(self.store, self.sid, "2026-09")
        self.assertEqual(first["history_total"], 1)
        confirmed = tax_review.save(self.store, self.sid, "2026-09", first["review"]["revision"], "已核对", True)
        self.assertTrue(confirmed["review"]["confirmed"])
        self.store.compare_tax(self.sid, {"rows": [row(amount=200)]}, opts)
        self.assertEqual(tax_review.read(self.store, self.sid, "2026-09")["history_total"], 1)
        snapshot = self.store.tax_snapshot(self.sid, opts)
        grouped = group_rows([{**snapshot[0], "shop_id": self.sid, "shop_name": "店一"}])
        self.assertFalse(grouped[0]["db_reviewed"])  # no highlighted change requires review
        self.assertTrue(tax_review.read(self.store, self.sid, "2026-09")["review"]["confirmed"])
        self.assertFalse(grouped[0]["db_changed"])
        self.store.compare_tax(self.sid, {"rows": [row(amount=300)]}, opts)
        current = tax_review.read(self.store, self.sid, "2026-09")
        self.assertFalse(current["review"]["confirmed"])
        self.assertEqual(current["history_total"], 1)
        with self.assertRaises(RuntimeError):
            tax_review.save(self.store, self.sid, "2026-09", first["review"]["revision"], "过期确认", True)
        self.assertEqual(len(tax_review.read(self.store, self.sid, "2026-09")["reviews"]), 1)
        with self.store.connect() as db:
            self.assertEqual(
                json.loads(db.execute("SELECT baseline FROM tax_records").fetchone()[0])["tax_amount_cents"], 200
            )
        self.store.update(self.sid, "new-test", "pw")
        with self.assertRaises(ValueError):
            tax_review.read(self.store, self.sid, "2026-09")

    def test_migrates_history_once_and_deletion_cleans_records(self):
        opts = tax_options("2026-09")
        self.store.compare_tax(self.sid, {"rows": [row()]}, opts)
        self.store.compare_tax(self.sid, {"rows": [row(amount=200)]}, opts)
        with self.store.connect() as db:
            db.execute("DELETE FROM tax_history")
            db.execute("DELETE FROM settings WHERE key='tax_history_migrated'")
        for _ in range(2):
            self.store = Store(self.temp.name)
        self.assertEqual(tax_review.read(self.store, self.sid, "2026-09")["history_total"], 2)
        self.store.delete(self.sid)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tax_history").fetchone()[0], 0)

    def test_product_does_not_mix_shops_scopes_or_name_only_matches(self):
        other = self.store.add("other", "pw")
        self.store.write_cache(
            self.sid, "products:one", {"rows": [{"id": "123", "goods_id": "123", "stock": 2}], "complete": True}
        )
        self.store.write_cache(
            other, "products:two", {"rows": [{"id": "123", "goods_id": "123", "stock": 999}], "complete": True}
        )
        self.store.write_cache(self.sid, "reviews:old", {"rows": [{"id": "old", "goods_id": "123"}], "complete": True})
        self.store.write_cache(
            self.sid,
            "reviews:new",
            {"rows": [{"id": "new", "goods_id": "123"}, {"id": "no-id", "name": "same name"}], "complete": False},
        )
        d = product_snapshot(self.store, self.sid, "123")
        self.assertEqual(d["sections"]["products"]["rows"][0]["stock"], 2)
        self.assertEqual([r["id"] for r in d["sections"]["reviews"]["rows"]], ["new"])
        self.assertFalse(d["sections"]["reviews"]["complete"])
        self.store.update(self.sid, "replacement", "pw")
        self.assertFalse(product_snapshot(self.store, self.sid, "123")["sections"]["products"]["updated_at"])

    def test_xlsx_literal_text_long_identifiers_and_finance_details(self):
        raw = workbook([("数据", [["订单", "文字", "金额"], ["998877665544332211", '=HYPERLINK("x")', 12.34]])])
        with ZipFile(io.BytesIO(raw)) as z:
            for name in z.namelist():
                ET.fromstring(z.read(name))
            sheet = z.read("xl/worksheets/sheet1.xml").decode()
            self.assertIn("998877665544332211", sheet)
            self.assertNotIn("<f>", sheet)
            self.assertIn("<v>12.34</v>", sheet)
        d = {
            "rows": [
                {
                    "shop_name": "店一",
                    "period": "2026-09",
                    "breakdown": {
                        "income": [{"label": "交易收入", "amount": 10, "count": 1}],
                        "outcome": [{"label": "退款", "amount": -1, "count": 1}],
                    },
                }
            ],
            "sources": [],
        }
        with ZipFile(io.BytesIO(response("reconciliation_month", d, {}).body)) as z:
            text = z.read("xl/worksheets/sheet4.xml").decode()
            self.assertIn("交易收入", text)
            self.assertIn("退款", text)

    def test_export_all_filtered_rows_not_just_page_and_api_authorization(self):
        records = [
            {
                "id": str(i),
                "goods_id": str(i),
                "name": "目标" if i < 25 else "其他",
                "shop_id": self.sid,
                "shop_name": "店一",
                "created_at": "2026-10-08",
            }
            for i in range(30)
        ]
        service = SimpleNamespace(data=lambda *a, **kw: {"rows": records.copy(), "sources": [], "metrics": []})
        app = FastAPI()
        app.include_router(create_router(service, lambda: None))
        with TestClient(app) as client:
            r = client.get("/api/data/products?keyword=目标&export=true&page=2&page_size=20")
            self.assertEqual(r.status_code, 200)
            with ZipFile(io.BytesIO(r.content)) as z:
                root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
                ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                self.assertEqual(len(root.findall("s:sheetData/s:row", ns)), 26)

        def deny():
            raise HTTPException(401, "unauthorized")

        protected = FastAPI()
        protected.include_router(workspace_router(service, self.store, deny))
        with TestClient(protected) as c:
            for path in ("todos", "product/1/123", "tax-review/1/2026-09"):
                self.assertEqual(c.get("/api/" + path).status_code, 401)

    def test_todos_priority_order_and_review_conflict(self):
        from backend import attention
        from backend.business import BusinessService

        service = BusinessService(self.store, lambda _: None)
        for kind, status in [
            ("aftersales", "平台处理中"),
            ("evidence", "待举证"),
            ("intercepts", "拦截失败"),
            ("tickets", "违规已处理"),
        ]:
            attention.publish(
                self.store,
                self.sid,
                kind,
                {
                    "rows": [{"id": kind, "status": status, "deadline": "2026-10-09" if kind == "evidence" else ""}],
                    "complete": True,
                },
            )
        app = FastAPI()
        app.include_router(workspace_router(service, self.store, lambda: None))
        self.store.compare_tax(self.sid, {"rows": [row()]}, tax_options("2026-09"))
        with TestClient(app) as c:
            self.assertEqual(
                [r["kind"] for r in c.get("/api/todos").json()["rows"]], ["aftersales", "evidence", "intercepts"]
            )
            self.assertEqual(
                c.post(
                    f"/api/tax-review/{self.sid}/2026-09", json={"revision": "0" * 64, "confirmed": True}
                ).status_code,
                409,
            )
            self.assertEqual(c.post(f"/api/tax-review/{self.sid}/2026-09", json={"revision": "bad"}).status_code, 422)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_queries_merge_and_cancel_cleans_queue(self):
        with tempfile.TemporaryDirectory() as path:
            store = Store(path)
            sid = store.add("test", "pw")
            record = store.list()[0]
            item = SimpleNamespace(shop=record, lock=asyncio.Lock(), state="logged_in", retired=False)
            service = BusinessService(store, lambda _: item)
            gate = asyncio.Event()

            async def collect(*_):
                await gate.wait()
                return {"rows": [], "complete": True}

            with patch("backend.business.collect", collect):
                service.request_sync("products", options_for("products"))
                service.request_sync("products", options_for("products"))
                self.assertEqual(len(service.jobs), 1)
                await asyncio.sleep(0)
                await service.cancel_shop(sid)
                self.assertEqual(service.progress, {})
                self.assertEqual(service.running_shops, set())
                service.request_sync("products", options_for("products"))
                await service.close()
                self.assertFalse(service.progress)

    async def test_waiting_same_shop_does_not_block_other_shops_or_gain_hidden_priority(self):
        service = BusinessService(None, None)
        from backend.tests.collection_fixture import policy

        service.configure_queue(policy(shop_concurrent=1, max_active_shops=3, max_concurrent=3))
        entered = []
        gates = {}
        items = {sid: SimpleNamespace(lock=asyncio.Lock()) for sid in range(1, 4)}

        async def task(sid, kind):
            async with service.slot((sid, kind), items[sid], kind):
                entered.append((sid, kind))
                await gates[(sid, kind)].wait()

        tasks = []
        for key in [(1, "old"), (1, "background"), (1, "focus"), (2, "other"), (3, "other")]:
            gates[key] = asyncio.Event()
            tasks.append(asyncio.create_task(task(*key)))
        await asyncio.sleep(0.01)
        self.assertEqual(entered, [(1, "old"), (2, "other"), (3, "other")])
        service.focus_kind = "focus"
        gates[(1, "old")].set()
        await asyncio.sleep(0.01)
        self.assertEqual(entered[-1], (1, "background"))
        for g in gates.values():
            g.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        await service.close()
        self.assertEqual(service.running_shops, set())
        self.assertEqual(service.progress, {})
