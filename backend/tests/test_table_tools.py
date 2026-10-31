import json, tempfile, unittest, zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from sqlalchemy import create_engine, select
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from types import SimpleNamespace
from backend.center import table_tools as t, report_view as v, reconciliation as r, database as d, cost_tables as c


class TableToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.engine = create_engine("sqlite:///" + str(self.root / "db"))
        for meta in (d.metadata, v.meta, r.meta, c.meta, t.meta):
            meta.create_all(self.engine)
        with self.engine.begin() as db:
            db.execute(
                d.settings.insert().values(
                    key="report-export:" + "a" * 32,
                    value=dict(id="a" * 32, import_state="ready", kind="orders", state="completed"),
                )
            )
            db.execute(
                v.documents.insert().values(
                    id="a" * 32,
                    shop_id=1,
                    shop_name="测试店",
                    start="2026-09-01 00:00:00",
                    end="2026-10-01 00:00:00",
                    ready=1,
                    created=1,
                    headers=["订单号", "商家实收金额(元)"],
                )
            )
            db.execute(
                v.rows.insert(),
                [
                    dict(
                        job_id="a" * 32,
                        position=i,
                        cells=["000123456789012345678" + str(i), str(i)],
                        amount=i if i != 3 else None,
                        search="目标" if i < 12 else "其他",
                        status="已完成",
                    )
                    for i in range(25)
                ],
            )
            db.execute(
                r.results.insert(),
                [
                    dict(job_id="a" * 32, position=i, cost={"total": str(i), "profit": str(i), "state": "已匹配"})
                    for i in range(25)
                ],
            )
        app = FastAPI()
        t.install(app, SimpleNamespace(engine=self.engine), lambda: None)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def test_server_sort_all_filter_totals_and_missing_last(self):
        result = t.query(self.engine, dict(keyword="目标", sort="raw_1", direction="desc", page_size=10))
        self.assertEqual(result["total"], 12)
        self.assertEqual(result["rows"][0]["raw_1"], "11.000000")
        self.assertEqual(result["summary"]["raw_1"]["missing"], 1)
        self.assertEqual(float(result["summary"]["raw_1"]["value"]), 63)
        last = t.query(self.engine, dict(keyword="目标", sort="raw_1", direction="desc", page_size=10, page=2))
        self.assertIsNone(last["rows"][-1]["raw_1"])
        self.assertEqual(t.query(self.engine, dict(filters={"cost_state": "已匹配"}))["total"], 25)

    def test_background_export_all_filtered_and_selected_not_page(self):
        for selection, expected in [
            (None, 12),
            ({"all": True, "ids": ["a" * 32 + ":0"]}, 11),
            ({"all": False, "ids": ["a" * 32 + ":2"]}, 1),
        ]:
            body = dict(
                source="orders",
                format="xlsx",
                title="测试",
                query=dict(keyword="目标", page=2, page_size=10),
                selection=selection,
            )
            response = self.client.post("/api/table-tools/exports", json=body)
            self.assertEqual(response.status_code, 200, response.text)
            jid = response.json()["id"]
            t.run_export(self.engine, jid, self.root)
            with self.engine.connect() as db:
                state = db.execute(select(t.exports.c.payload).where(t.exports.c.id == jid)).scalar()
                self.assertEqual(state["state"], "completed", state)
            with zipfile.ZipFile(self.root / (jid + ".xlsx")) as z:
                sheet = z.read("xl/worksheets/sheet1.xml")
                root = ET.fromstring(sheet)
                ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                self.assertEqual(len(root.findall(".//s:row", ns)), expected + 1)
                self.assertIn(b"000123456789012345678", sheet)

    def test_inline_excel_literal_text_and_empty_zero_distinction(self):
        body = dict(
            format="xlsx",
            title="成本",
            columns=[dict(id="sku", label="SKU", type="text"), dict(id="cost", label="成本", type="number")],
            rows=[dict(sku="001", cost=0), dict(sku="=1+1", cost=None)],
        )
        jid = self.client.post("/api/table-tools/exports", json=body).json()["id"]
        t.run_export(self.engine, jid, self.root)
        with zipfile.ZipFile(self.root / (jid + ".xlsx")) as z:
            sheet = z.read("xl/worksheets/sheet1.xml")
            self.assertIn(b"001", sheet)
            self.assertIn(b"=1+1", sheet)
            self.assertNotIn(b"<f>", sheet)
            self.assertIn(b"<v>0.0</v>", sheet)

    def test_authentication(self):
        def denied():
            raise HTTPException(401, "Denied")

        app = FastAPI()
        t.install(app, SimpleNamespace(engine=self.engine), denied)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/table-tools/exports").status_code, 401)
