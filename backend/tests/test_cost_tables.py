import base64, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from backend.center import cost_tables as costs


class CostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.temp.name) / "test.db"))
        app = FastAPI()
        costs.install(app, SimpleNamespace(engine=self.engine), lambda: None)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.temp.cleanup()

    def upload(self, text):
        return {"filename": "成本.csv", "content": base64.b64encode(text.encode("utf-8-sig")).decode()}

    def test_validate_duplicate_negative_and_missing_columns(self):
        for text in ("SKU编码,单位成本\n001,1\n001,2", "SKU编码,单位成本\n001,-1", "商品,价格\n一,2"):
            data = self.upload(text)
            p = self.client.post("/api/cost-tables/preview", json=data)
            self.assertFalse(p.json()["valid"])
            self.assertEqual(self.client.post("/api/cost-tables", json=data).status_code, 400)
        self.assertEqual(self.client.get("/api/cost-tables").json()["books"], [])

    def test_activate_replace_delete_and_exact_decimal_matching(self):
        old = self.client.post("/api/cost-tables", json=self.upload("SKU编码,单位成本\n001,1.125\nZERO,0")).json()["id"]
        result = {
            "headers": ["商家编码-规格维度", "商品数量(件)", "商家实收金额(元)"],
            "rows": [
                {"cells": ["001", "3", "10"]},
                {"cells": ["1", "2", "10"]},
                {"cells": ["ZERO", "2", "10"]},
                {"cells": ["001", "", "10"]},
            ],
        }
        out = costs.enrich(self.engine, result)["rows"]
        self.assertEqual(float(out[0]["cost"]["total"]), 3.375)
        self.assertEqual(float(out[0]["cost"]["profit"]), 6.625)
        self.assertIsNone(out[1]["cost"]["profit"])
        self.assertEqual(float(out[2]["cost"]["profit"]), 10)
        self.assertIsNone(out[3]["cost"]["profit"])
        new = self.client.post("/api/cost-tables", json=self.upload("SKU编码,单位成本\n001,3")).json()["id"]
        self.assertEqual(self.client.get("/api/cost-tables").json()["active_id"], new)
        self.client.post("/api/cost-tables/" + old + "/activate")
        self.assertEqual(self.client.get("/api/cost-tables").json()["active_id"], old)
        self.client.delete("/api/cost-tables/" + old)
        self.assertIsNone(self.client.get("/api/cost-tables").json()["active_id"])
        self.assertEqual(len(self.client.get("/api/cost-tables").json()["books"]), 1)

    def test_mapping_preview_and_csv_leading_zeros(self):
        data = {**self.upload("货号,采购金额\n00001,12.34"), "sku_column": 0, "cost_column": 1}
        p = self.client.post("/api/cost-tables/preview", json=data).json()
        self.assertTrue(p["valid"])
        self.assertEqual(p["sample"][0][0], "00001")
        bid = self.client.post("/api/cost-tables", json=data).json()["id"]
        self.assertEqual(self.client.get("/api/cost-tables/" + bid + "?keyword=00001").json()["total"], 1)

    def test_authorization_on_all_routes(self):
        def deny():
            raise HTTPException(401, "need login")

        app = FastAPI()
        costs.install(app, SimpleNamespace(engine=self.engine), deny)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/cost-tables").status_code, 401)
            self.assertEqual(
                client.post("/api/cost-tables", json=self.upload("SKU编码,单位成本\na,1")).status_code, 401
            )

    def test_xlsx_text_sku_and_invalid_workbook(self):
        import io, zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr(
                "xl/worksheets/sheet1.xml",
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1" t="inlineStr"><is><t>SKU编码</t></is></c><c r="B1" t="inlineStr"><is><t>单位成本</t></is></c></row><row><c r="A2" t="inlineStr"><is><t>00001</t></is></c><c r="B2"><v>12.50</v></c></row></sheetData></worksheet>',
            )
        body = {"filename": "成本.xlsx", "content": base64.b64encode(buffer.getvalue()).decode()}
        result = self.client.post("/api/cost-tables/preview", json=body).json()
        self.assertTrue(result["valid"])
        self.assertEqual(result["sample"][0], ["00001", "12.50"])
        self.assertEqual(
            self.client.post(
                "/api/cost-tables/preview", json={**body, "content": base64.b64encode(b"bad workbook").decode()}
            ).status_code,
            400,
        )
