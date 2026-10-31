import base64, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from sqlalchemy import create_engine, select
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.center import cost_tables as c, cost_supplements as s, reconciliation as r, report_view as v


def upload(text):
    return c.Upload(
        filename="补充.csv",
        content=base64.b64encode(text.encode()).decode(),
        sku_column=0,
        cost_column=3,
        shipping_column=4,
    )


class SupplementTests(unittest.TestCase):
    def test_manual_sku_scoped_persistent_and_recalculates_without_editing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_engine("sqlite:///" + str(Path(tmp) / "test.db"))
            app = FastAPI()
            c.install(app, SimpleNamespace(engine=engine), lambda: None)
            v.meta.create_all(engine)
            client = TestClient(app)
            client.post(
                "/api/cost-tables",
                json={
                    "filename": "原成本.csv",
                    "content": base64.b64encode("商家编码,成本价,快递费\nABC,2,1\nXYZ,4,1".encode()).decode(),
                    "shipping_column": 2,
                },
            )
            headers = ["商家编码-规格维度", "商品数量(件)", "商家实收金额(元)", "订单号", "商品", "商品规格"]
            with engine.begin() as db:
                for shop, jid in [(1, "a" * 32), (2, "b" * 32)]:
                    db.execute(
                        v.documents.insert().values(
                            id=jid,
                            shop_id=shop,
                            shop_name="店铺" + str(shop),
                            start="2026-09-01 00:00:00",
                            end="2026-10-01 00:00:00",
                            ready=1,
                            headers=headers,
                            row_count=2,
                        )
                    )
                    db.execute(
                        v.rows.insert(),
                        [
                            dict(job_id=jid, position=i, cells=[sku, "1", "10", str(i), "商品", "规格"])
                            for i, sku in enumerate(["", "XYZ"])
                        ],
                    )
            r.snapshot(engine, "a" * 32)
            r.snapshot(engine, "b" * 32)
            self.assertEqual(len(s.missing(engine)[0]["scopes"]), 2)
            body = dict(shop_id=1, product="商品", spec="规格", sku="abc")
            response = client.post("/api/cost-supplements/manual-sku", json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(response.json()["tasks"]), 1)
            s.process(engine, response.json()["id"])
            calculated = r.snapshot(engine, "a" * 32)
            self.assertEqual(calculated["summary"]["unmatched"], 0)
            self.assertEqual(calculated["summary"]["net"], "12.000000")
            self.assertEqual(r.snapshot(engine, "b" * 32)["summary"]["unmatched"], 1)
            with engine.connect() as db:
                self.assertEqual(
                    db.execute(
                        select(v.rows.c.cells).where(v.rows.c.job_id == "a" * 32, v.rows.c.position == 0)
                    ).scalar()[0],
                    "",
                )
                costs = list(
                    db.execute(
                        select(r.results.c.cost).where(r.results.c.job_id == "a" * 32).order_by(r.results.c.position)
                    ).scalars()
                )
                self.assertEqual(costs[0]["sku"], "abc")
                self.assertEqual(costs[1]["sku"], "XYZ")
            links = client.get("/api/cost-supplements").json()["sku_links"]
            self.assertEqual(len(links), 1)
            self.assertEqual(
                client.post("/api/cost-supplements/manual-sku", json={**body, "sku": "missing"}).status_code, 400
            )
            changed = client.post(
                "/api/cost-supplements/manual-sku", json={**body, "sku": "missing", "updated": links[0]["updated"]}
            ).json()
            s.process(engine, changed["id"])
            self.assertEqual(r.snapshot(engine, "a" * 32)["summary"]["unmatched"], 1)
            with engine.connect() as db:
                self.assertEqual(db.execute(select(s.sku_links.c.history)).scalar()[0]["sku"], "abc")
            self.assertEqual(
                client.post("/api/cost-supplements/manual-sku", json={**body, "shop_id": 999}).status_code, 400
            )
            client.close()
            engine.dispose()

    def test_fill_only_missing_preserve_history_retry_and_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_engine("sqlite:///" + str(Path(tmp) / "test.db"))
            app = FastAPI()
            c.install(app, SimpleNamespace(engine=engine), lambda: None)
            v.meta.create_all(engine)
            client = TestClient(app)
            first = client.post(
                "/api/cost-tables",
                json={
                    "filename": "原成本.csv",
                    "content": base64.b64encode("商家编码,成本价,快递费\nOLD,2,1".encode()).decode(),
                    "shipping_column": 2,
                },
            ).json()["id"]
            headers = ["商家编码-规格维度", "商品数量(件)", "商家实收金额(元)", "订单号", "商品", "商品规格"]
            with engine.begin() as db:
                db.execute(
                    v.documents.insert().values(
                        id="a" * 32,
                        shop_id=1,
                        shop_name="店铺A",
                        start="2026-09-01 00:00:00",
                        end="2026-10-01 00:00:00",
                        ready=1,
                        headers=headers,
                        row_count=2,
                    )
                )
                db.execute(
                    v.rows.insert(),
                    [
                        dict(job_id="a" * 32, position=i, cells=[sku, "1", "10", str(i), "商品", "规格"])
                        for i, sku in enumerate(["OLD", "new"])
                    ],
                )
            before = r.snapshot(engine, "a" * 32)
            self.assertEqual(before["summary"]["unmatched"], 1)
            client.post(
                "/api/cost-tables",
                json={
                    "filename": "新成本.csv",
                    "content": base64.b64encode("商家编码,成本价,快递费\nOLD,9,1".encode()).decode(),
                    "shipping_column": 2,
                },
            )
            self.assertEqual(s.missing(engine)[0]["sku"], "new")
            template = client.get("/api/cost-supplements/template")
            self.assertEqual(template.status_code, 200)
            self.assertIn("new", template.content.decode("utf-8-sig"))
            body = upload("商家编码,商品名称,规格,成本价,快递费\nNEW,商品,规格,3,1")
            job = s.create(engine, body)
            self.assertEqual(len(job["tasks"]), 1)
            self.assertEqual(s.create(engine, body)["id"], job["id"])
            with patch.object(s, "snapshot", side_effect=RuntimeError("临时写入失败")):
                s.process(engine, job["id"])
            with engine.connect() as db:
                self.assertEqual(db.execute(select(s.jobs.c.payload)).scalar()["state"], "error")
            self.assertEqual(client.post("/api/cost-supplements/" + job["id"] + "/retry", json={}).status_code, 200)
            s.process(engine, job["id"])
            s.process(engine, job["id"])
            after = r.snapshot(engine, "a" * 32)
            self.assertNotEqual(after["book_id"], first)
            self.assertEqual(after["summary"]["product_cost"], "5.000000")
            self.assertEqual(after["summary"]["unmatched"], 0)
            with engine.connect() as db:
                self.assertEqual(
                    db.execute(select(c.entries.c.unit_cost).where(c.entries.c.book_id == first)).scalar(), 2
                )
                active = db.execute(select(c.catalog.c.active_id)).scalar()
                self.assertEqual(
                    sorted(db.execute(select(c.entries.c.unit_cost).where(c.entries.c.book_id == active)).scalars()),
                    [3, 9],
                )
            self.assertEqual(client.delete("/api/cost-tables/" + after["book_id"]).status_code, 409)
            with self.assertRaises(ValueError):
                s.create(engine, upload("商家编码,商品名称,规格,成本价,快递费\n,,规格,3,1"))
            self.assertEqual(s.missing(engine), [])
            client.close()
            engine.dispose()
