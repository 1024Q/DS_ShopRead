import base64, io, tempfile, unittest, zipfile
from pathlib import Path
from decimal import Decimal as D
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from backend.center import (
    reconciliation as account,
    cost_tables as cost,
    report_view as report,
    promotion_ledger as promo,
)
from backend.report_export_store import Ledger

H = ["商家编码-规格维度", "商品数量(件)", "商家实收金额(元)", "订单号", "快递单号", "商品", "商品规格"]


def row(pos, sku, qty, amount, order="order", package="package"):
    return {"position": pos, "cells": [sku, str(qty), str(amount), order, package, "商品", "规格"]}


class AccountingTests(unittest.TestCase):
    def test_case_insensitive_sku_conflicts_and_surcharge(self):
        costs = {"ABC": {"unit_cost": D("2"), "cells": ["ABC", "2", "3"]}}
        records = [row(1, "abc", 2, 10, "a"), row(2, "ABC", 1, 5, "b"), row(3, "missing", 1, 4, "c")]
        records[1]["cells"][5] = "【购买前联系客服】补收差价专用商品"
        out, s = account.calculate(records, H, costs, {"shipping_column": 2, "shipping_mode": "piece"})
        self.assertEqual(out[0]["total"], "4")
        self.assertEqual(out[0]["shipping"], "6")
        self.assertEqual(out[1]["total"], "0")
        self.assertEqual(out[1]["shipping"], "0")
        self.assertEqual(out[1]["net"], "5")
        self.assertIn("补差价", out[1]["state"])
        self.assertIsNone(out[2]["total"])
        self.assertEqual(s["unmatched"], 1)
        costs["abc"] = {"unit_cost": D("9"), "cells": ["abc", "9", "1"]}
        out, _ = account.calculate([records[0]], H, costs, {})
        self.assertIsNone(out[0]["total"])
        self.assertIn("重复", out[0]["state"])

    def test_initial_cost_binding_recovers_without_replacing_existing_version(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = create_engine("sqlite:///" + str(Path(folder) / "test.db"))
            app = FastAPI()
            cost.install(app, SimpleNamespace(engine=engine), lambda: None)
            report.meta.create_all(engine)
            with engine.begin() as db:
                db.execute(
                    report.documents.insert().values(
                        id="f" * 32,
                        shop_id=1,
                        shop_name="店铺",
                        start="2026-09-01 00:00:00",
                        end="2026-10-01 00:00:00",
                        headers=H,
                        row_count=1,
                        ready=1,
                    )
                )
                db.execute(
                    report.rows.insert().values(job_id="f" * 32, position=1, cells=row(1, "abc", 2, 10)["cells"])
                )
            self.assertIsNone(account.snapshot(engine, "f" * 32)["book_id"])
            with TestClient(app) as client:

                def upload(price):
                    return client.post(
                        "/api/cost-tables",
                        json={
                            "filename": "成本.csv",
                            "content": base64.b64encode(("商家编码,成本价\nABC," + str(price)).encode()).decode(),
                        },
                    ).json()["id"]

                bid = upload(2)
                new = account.snapshot(engine, "f" * 32)
                self.assertEqual(new["book_id"], bid)
                self.assertEqual(new["summary"]["unmatched"], 0)
                upload(9)
                self.assertEqual(account.snapshot(engine, "f" * 32)["book_id"], bid)
                refreshed = account.snapshot(engine, "f" * 32, refresh_rules=True)
                self.assertEqual(refreshed["book_id"], bid)
                self.assertEqual(refreshed["summary"]["product_cost"], "4.000000")
                bad = client.post(
                    "/api/cost-tables",
                    json={
                        "filename": "成本.csv",
                        "content": base64.b64encode("商家编码,成本价\nABC,2\nabc,3".encode()).decode(),
                    },
                )
                self.assertEqual(bad.status_code, 400)
            engine.dispose()

    def test_shipping_once_and_order_revenue_deduplicated(self):
        c = {
            "001": {"unit_cost": D("2"), "cells": ["001", "2", "3"]},
            "002": {"unit_cost": D("4"), "cells": ["002", "4", "5"]},
        }
        out, s = account.calculate(
            [row(1, "001", 2, 30), row(2, "002", 1, 30)],
            H,
            c,
            {"shipping_column": 2, "shipping_mode": "package"},
            "order",
        )
        self.assertEqual(D(s["revenue"]), 30)
        self.assertEqual(D(s["product_cost"]), 8)
        self.assertEqual(D(s["shipping"]), 5)
        self.assertEqual(D(s["net"]), 17)
        self.assertEqual(D(out[0]["shipping"]) + D(out[1]["shipping"]), 5)
        _, uncertain = account.calculate([row(1, "001", 2, 30), row(2, "002", 1, 30)], H, c, {}, "unverified")
        self.assertIsNone(uncertain["profit"])
        self.assertEqual(uncertain["ambiguous_income_rows"], 2)
        self.assertFalse(uncertain["revenue_complete"])

    def test_missing_and_zero_cost_are_distinct(self):
        c = {"001": {"unit_cost": D(0), "cells": ["001", "0", "0"]}}
        out, s = account.calculate(
            [row(1, "001", 2, 10, "a"), row(2, "1", 1, 20, "b")], H, c, {"shipping_column": 2, "shipping_mode": "piece"}
        )
        self.assertEqual(D(out[0]["total"]), 0)
        self.assertIsNone(out[1]["total"])
        self.assertIsNone(s["profit"])
        self.assertEqual(s["unmatched"], 1)

    def test_cost_version_pinned_and_recalculated(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = create_engine("sqlite:///" + str(Path(folder) / "test.db"))
            app = FastAPI()
            cost.install(app, SimpleNamespace(engine=engine), lambda: None)
            report.meta.create_all(engine)
            client = TestClient(app)

            def upload(price):
                return client.post(
                    "/api/cost-tables",
                    json={
                        "filename": "成本.csv",
                        "content": base64.b64encode(
                            ("商家编码,成本价,快递费\n001," + str(price) + ",3").encode()
                        ).decode(),
                        "shipping_column": 2,
                        "shipping_mode": "order",
                    },
                ).json()["id"]

            first = upload(2)
            with engine.begin() as db:
                db.execute(
                    report.documents.insert().values(
                        id="a" * 32,
                        shop_id=1,
                        shop_name="店铺",
                        start="2026-09-01 00:00:00",
                        end="2026-10-01 00:00:00",
                        headers=H,
                        row_count=1,
                        ready=1,
                    )
                )
                db.execute(
                    report.rows.insert().values(job_id="a" * 32, position=1, cells=row(1, "001", 2, 10)["cells"])
                )
            x = account.snapshot(engine, "a" * 32)
            self.assertEqual(x["book_id"], first)
            self.assertEqual(D(x["summary"]["net"]), 3)
            second = upload(4)
            self.assertEqual(account.snapshot(engine, "a" * 32)["book_id"], first)
            self.assertEqual(client.delete("/api/cost-tables/" + first).status_code, 409)
            y = account.snapshot(engine, "a" * 32, True, "row")
            self.assertEqual(y["book_id"], second)
            self.assertEqual(D(y["summary"]["net"]), -1)
            self.assertEqual(account.unmatched(engine, ["a" * 32])["total"], 0)
            client.close()
            engine.dispose()

    def test_monthly_file_scope_balance_and_duplicate_guard(self):
        content = "月份,资金类型,本月余额,上月余额,本月收入,本月支出\n2026-09,现金,20,10,100,90\n2026-09,红包,0,0,5,5\n2026-08,现金,10,5,20,15\n"
        missing, _ = promo.parse(
            content.replace("2026-09,现金,20,10,100,90", "2026-09,现金,20,10,-,90").encode(), ".csv", ""
        )
        self.assertIsNone(missing[0][4])
        rows, total = promo.parse(content.encode(), ".csv", "2026-09")
        self.assertEqual(len(rows), 2)
        self.assertEqual(D(total["本月收入"]), 105)
        with self.assertRaises(ValueError):
            promo.parse((content + "2026-09,现金,20,10,100,90\n").encode(), ".csv", "2026-09")
        with self.assertRaises(ValueError):
            promo.parse(content.replace("20,10,100,90", "21,10,100,90").encode(), ".csv", "2026-09")
        with self.assertRaises(ValueError):
            promo.parse(content.encode(), ".csv", "2026-07")

    def test_promotion_jobs_are_separate_and_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(folder)
            shop = {"id": 1}
            scope = ("2026-09-01 00:00:00", "2026-10-01 00:00:00")
            from unittest.mock import patch

            # Promotion scope is a complete month and independent of order report fields.
            a = ledger.create(shop, *scope, kind="promotion")
            b = ledger.create(shop, *scope, kind="promotion")
            self.assertEqual(a["id"], b["id"])
            self.assertEqual(a["kind"], "promotion")
            self.assertEqual(a["fields"], ["多多推广月账单"])
            with self.assertRaises(ValueError):
                ledger.create(shop, "2026-09-02 00:00:00", scope[1], kind="promotion")

    def test_all_months_import_and_latest_per_shop_month(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = create_engine("sqlite:///" + str(Path(folder) / "test.db"))
            p = Path(folder) / "report.zip"
            text = (
                "月份,资金类型,本月余额,上月余额,本月收入,本月支出\n2026-09,现金,20,10,100,90\n2026-08,现金,10,5,20,15"
            )
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("账单.csv", text)
            job = {"id": "c" * 32, "shop_id": 1, "shop_name": "店铺", "start": "all", "end": "all", "created": 1}
            self.assertEqual(promo.import_report(engine, job, p), 2)
            self.assertEqual(promo.import_report(engine, {**job, "id": "d" * 32, "created": 2}, p), 2)
            app = FastAPI()
            promo.install(app, SimpleNamespace(engine=engine), lambda: None)
            with TestClient(app) as client:
                all_rows = client.get("/api/promotion-ledger").json()["bills"]
                self.assertEqual(len(all_rows), 2)
                self.assertEqual({b["month"] for b in all_rows}, {"2026-08", "2026-09"})
                self.assertEqual({b["job_id"] for b in all_rows}, {"d" * 32})
                one = client.get("/api/promotion-ledger?month=2026-08").json()["bills"]
                self.assertEqual(len(one), 1)
                self.assertEqual(one[0]["rows"][0][0], "2026-08")
            engine.dispose()

    def test_month_import_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = create_engine("sqlite:///" + str(Path(folder) / "test.db"))
            p = Path(folder) / "report.zip"
            job = {"id": "b" * 32, "shop_id": 2, "shop_name": "店铺", "start": "2026-09-01 00:00:00", "created": 1}
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("账单.csv", "月份,资金类型,本月余额,上月余额,本月收入,本月支出\n2026-09,现金,20,10,100,90")
            self.assertEqual(promo.import_report(engine, job, p), 1)
            self.assertEqual(promo.import_report(engine, job, p), 1)
            with engine.connect() as db:
                self.assertEqual(len(db.execute(select(promo.bills)).all()), 1)
            engine.dispose()


class PromotionPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_node_promotion_download_pack_and_status(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from backend.report_exports import Worker

        with tempfile.TemporaryDirectory() as folder:
            store = SimpleNamespace(directory=Path(folder))
            item = SimpleNamespace(shop={"id": 1, "platform": "pdd"}, state="logged_in", lock=asyncio.Lock())
            worker = Worker(store, lambda sid: item)
            job = worker.ledger.create(item.shop, "2026-09-01 00:00:00", "2026-10-01 00:00:00", kind="promotion")
            original = worker.ledger.path(job["id"]) / "original.download"
            original.write_text(
                "月份,资金类型,本月余额,上月余额,本月收入,本月支出\n2026-09,现金,20,10,100,90", encoding="utf-8"
            )
            context = AsyncMock()
            context.__aenter__.return_value = AsyncMock()
            with (
                patch("backend.report_exports.adapter.OrdersPage", return_value=context),
                patch("backend.promotion_export.collect", new_callable=AsyncMock, return_value=original),
                patch("backend.operation_log.outcome"),
            ):
                await worker.process(job)
            done = worker.ledger.get(job["id"])
            self.assertEqual(done["state"], "completed")
            self.assertGreater(done["size"], 0)
            self.assertEqual(len(done["sha256"]), 64)


class PromotionCenterTests(unittest.TestCase):
    def test_create_routes_kind_owner_and_list_isolation(self):
        import time
        from unittest.mock import patch, AsyncMock
        from backend.center.database import Database
        from backend.center import report_exports as center

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = Database("sqlite:///" + str(root / "db"))
            node = db.register("节点")
            db.ingest(
                node["id"],
                [
                    {
                        "shop": "7",
                        "scope": "shop_metadata",
                        "updated": time.time(),
                        "payload": {"name": "店铺", "platform": "pdd"},
                    }
                ],
            )
            shop = db.shops()[0]
            app = FastAPI()
            with patch.object(center, "ROOT", root):
                cost.install(app, db, lambda: None)
                center.install(
                    app,
                    db,
                    SimpleNamespace(
                        peers={node["id"]: True}, request=AsyncMock(return_value={"status": 200, "content": b"{}"})
                    ),
                    lambda: None,
                )
                client = TestClient(app)
                response = client.post(
                    "/api/report-exports", json={"kind": "promotion", "shop_ids": [shop["id"]], "confirmed": True}
                )
                self.assertEqual(response.status_code, 200, response.text)
                job = response.json()["jobs"][0]
                self.assertEqual(job["local_shop_id"], 7)
                self.assertEqual(job["kind"], "promotion")
                self.assertEqual((job["start"], job["end"]), ("all", "all"))
                self.assertEqual(len(client.get("/api/report-exports?kind=promotion").json()["jobs"]), 1)
                self.assertEqual(client.get("/api/report-exports").json()["jobs"], [])
                self.assertEqual(client.get("/api/promotion-ledger").status_code, 200)
                self.assertEqual(client.delete("/api/report-exports/" + job["id"]).status_code, 200)
                client.close()
            db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
