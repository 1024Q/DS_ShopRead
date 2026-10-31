import tempfile
import io
from zipfile import ZipFile
from xml.etree import ElementTree as ET
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from backend.store import Store
from backend.browser import Session
from backend.business import BusinessService, create_router, options_for
from backend.workspace_api import create_router as workspace_router
from backend import data_versions, attention


class MultiScope(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.ids = [
            self.store.add(str(i), "test", name="同名店铺", platform=p)
            for i, p in enumerate(("pdd", "taobao", "pdd", "jd"))
        ]
        self.sessions = {s["id"]: Session(s, self.store) for s in self.store.list()}
        for session in self.sessions.values():
            session.state = "logged_in"
        self.service = BusinessService(self.store, self.sessions.__getitem__)
        app = FastAPI()
        app.include_router(create_router(self.service, lambda: None))
        app.include_router(workspace_router(self.service, self.store, lambda: None))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")
        for s in self.store.list():
            for kind in ("overview", "products"):
                options = self.service.query_options(kind, options_for(kind), s)
                self.store.write_cache(
                    s["id"],
                    self.service.key(kind, options, s),
                    {
                        "rows": [{"id": f"{s['id']}-{i}", "name": f"商品{i}", "status": "在售中"} for i in range(3)]
                        if kind == "products"
                        else [],
                        "metrics": {"sales": s["id"] * 10, "orders": s["id"]},
                        "complete": True,
                    },
                )
            self.store.log_event("error", "products", "fixture", "test", s["id"])

    async def asyncTearDown(self):
        await self.service.close()
        await self.client.aclose()
        self.temp.cleanup()

    async def test_intersection_totals_pagination_and_export(self):
        a, b, c, _ = self.ids
        q = {"shop_id": f"{a},{b},{a}", "platform": "pdd,taobao"}
        response = await self.client.get("/api/data/overview", params=q)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual({s["shop_id"] for s in response.json()["sources"]}, {a, b})
        self.assertEqual(response.json()["totals"]["sales"], (a + b) * 10)
        product_q = {**q, "shop_id": f"{a},{c}"}
        first = (await self.client.get("/api/data/products", params={**product_q, "page_size": 2})).json()
        second = (await self.client.get("/api/data/products", params={**product_q, "page_size": 2, "page": 2})).json()
        exported = await self.client.get("/api/data/products", params={**product_q, "export": True})
        self.assertEqual(first["total"], 6)
        self.assertTrue({r["id"] for r in first["rows"]}.isdisjoint(r["id"] for r in second["rows"]))
        self.assertEqual(exported.status_code, 200)
        with ZipFile(io.BytesIO(exported.content)) as book:
            sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
            self.assertEqual(len(sheet.findall(".//{*}sheetData/{*}row")), 7)
        self.assertEqual({r["shop_id"] for r in first["rows"] + second["rows"]}, {a, c})
        only = (await self.client.get("/api/data/overview", params={**q, "platform": "pdd"})).json()
        self.assertEqual({s["shop_id"] for s in only["sources"]}, {a})
        for invalid in ("oops", "1,,2", "-1"):
            self.assertEqual(
                (await self.client.get("/api/data/products", params={"shop_id": invalid})).status_code, 422
            )

    async def test_sync_and_attention_scope_do_not_expand(self):
        a, b, c, _ = self.ids
        with patch.object(self.service, "enqueue") as enqueue:
            response = await self.client.post(
                "/api/data/overview/sync", params={"shop_id": f"{a},{b}", "platform": "pdd,taobao", "force": True}
            )
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual({call.args[0]["id"] for call in enqueue.call_args_list}, {a, b})
        result = attention.read(self.service, "pdd,taobao", f"{a},{b}")
        self.assertEqual({s["shop_id"] for s in result["sources"]}, {a})
        response = await self.client.get("/api/todos", params={"shop_id": f"{a},{b}", "platform": "pdd,taobao"})
        self.assertEqual(response.status_code, 200, response.text)

    async def test_logs_and_cache_revisions_match_any_selected_shop(self):
        a, b, c, _ = self.ids
        from backend.log_spool import get as log_spool

        log_spool(self.store).flush(self.store)
        self.assertEqual({r["shop_id"] for r in self.store.read_logs(shop_id=f"{a},{b}")}, {a, b})
        q = {"shop_id": f"{a},{b}", "platform": "pdd,taobao"}
        before = data_versions.revision(self.store, "/api/data/products", q)
        with self.store.connect() as db:
            data_versions.publish(db, c, "products")
        self.assertEqual(data_versions.revision(self.store, "/api/data/products", q), before)
        with self.store.connect() as db:
            data_versions.publish(db, b, "products")
        self.assertGreater(data_versions.revision(self.store, "/api/data/products", q), before)
