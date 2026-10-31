import asyncio, io, json, tempfile, unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend import promotions, collection
from backend.promotions import options_for, fields, summarize, product, read_rows
from backend.store import Store
from backend.business import BusinessService, create_router
from backend.browser import Session
from backend.remote_nodes import PATH


def metric(spend=10, revenue=20):
    return {
        "orderSpend": {"unit": "YUAN", "value": str(spend)},
        "gmv": {"unit": "YUAN", "value": str(revenue)},
        "orderSpendRoiUnified": {"unit": "PER_ONE", "value": "2"},
        "spend": {"unit": "YUAN", "value": str(spend)},
    }


def raw(identifier=1):
    return {
        "adId": identifier,
        "goodsId": 100 + identifier,
        "adStatus": 1,
        "maxCost": {"unit": "YUAN", "value": "1000000"},
        "targetRoi": {"unit": "PER_ONE", "value": "2"},
        "goodsInfo": {"goodsName": "测试商品", "thumbUrl": "https://example.test/image.png"},
        "reportInfo": metric(),
    }


class PromotionTests(unittest.TestCase):
    def test_dates_daily_policy_and_invalid_ranges(self):
        options = options_for("week", date(2026, 1, 1))
        self.assertEqual(options["date_from"], "2025-12-25")
        self.assertEqual(options["date_to"], "2025-12-31")
        self.assertEqual(collection.mode("ad_reports", options, collection.DEFAULTS), "daily")
        self.assertEqual(collection.mode("ad_products", options_for(), collection.DEFAULTS), "realtime")
        with self.assertRaises(ValueError):
            options_for("invalid")

    def test_units_missing_zero_and_weighted_roi(self):
        self.assertEqual(fields(metric())["ad_spend"], 10)
        self.assertIsNone(fields(metric(0, 0))["roi"])
        self.assertIsNone(fields({})["ad_revenue"])
        self.assertAlmostEqual(
            summarize([{"ad_spend": 10, "ad_revenue": 100, "roi": 10}, {"ad_spend": 90, "ad_revenue": 90, "roi": 1}])[
                "roi"
            ],
            1.9,
        )
        self.assertEqual(
            summarize([{"ad_spend": 10, "ad_revenue": 20}, {"ad_spend": None, "ad_revenue": 100}])["roi"], 2
        )
        with self.assertRaises(ValueError):
            fields({"orderSpend": {"unit": "FEN", "value": "100"}})
        with self.assertRaises(ValueError):
            fields({"orderSpend": {"unit": "YUAN", "value": "NaN"}})

    def test_product_picture_identifier_and_unknown_status_preserved(self):
        r = product(raw(778850300749571016), "ad_products", 1, options_for())
        self.assertEqual(r["id"], "778850300749571016")
        self.assertTrue(r["thumbnail_url"])
        self.assertEqual(r["budget_text"], "不限")
        self.assertEqual(product({**raw(), "adStatus": 999}, "ad_products", 3, options_for())["status"], "未知推广状态")

    def test_api_filters_dashboard_export_and_remote_routes(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("test", "pw")
            session = Session(store.list()[0], store)
            session.state = "logged_in"
            service = BusinessService(store, lambda _: session)
            opts = options_for()
            payload = {
                "rows": [product(raw(), "ad_products", 1, opts)],
                "metrics": {**fields(metric()), "daily": [{"date": opts["date_to"], **fields(metric())}]},
                "complete": True,
                "source_total": 1,
            }
            for kind in promotions.KINDS:
                store.write_cache(sid, service.key(kind, opts, session.shop), payload)
            app = FastAPI()
            app.include_router(create_router(service, lambda: None))
            client = TestClient(app)
            with client:
                r = client.get("/api/data/ad_products?keyword=测试")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["total"], 1)
                self.assertEqual(client.get("/api/data/ad_products?keyword=missing").json()["total"], 0)
                r = client.get("/api/data/ad_reports?dashboard=true").json()
                self.assertEqual(r["ad_totals"]["roi"], 2)
                self.assertEqual(r["rows"], [])
                self.assertNotIn("daily", r["metrics"][0])
                response = client.get("/api/data/ad_reports?export=true")
                self.assertEqual(response.status_code, 200)
                with ZipFile(io.BytesIO(response.content)) as z:
                    self.assertIn("分日报表", z.read("xl/workbook.xml").decode())
            self.assertTrue(PATH.fullmatch("data/ad_reports"))
            self.assertTrue(PATH.fullmatch("shops/1/ad_products"))


class PagingTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_paging_and_missing_page_never_claim_complete(self):
        p = SimpleNamespace(
            wait=AsyncMock(
                side_effect=[({"adInfos": [raw()], "totalAdNum": 2}, {}), ({"adInfos": [raw(2)], "totalAdNum": 2}, {})]
            ),
            evaluate=AsyncMock(return_value=True),
            session=SimpleNamespace(read_progress=None),
        )
        rows, total, complete = await read_rows(p, "ad_products", options_for(), 1)
        self.assertEqual(len(rows), 2)
        self.assertTrue(complete)
        p.evaluate.assert_awaited_once()
        p.wait = AsyncMock(return_value=({"adInfos": [raw()], "totalAdNum": 2}, {}))
        p.evaluate = AsyncMock(return_value=False)
        rows, total, complete = await read_rows(p, "ad_products", options_for(), 1)
        self.assertFalse(complete)

    async def test_unenabled_managed_list_null_with_zero_total_is_empty(self):
        p = SimpleNamespace(
            wait=AsyncMock(return_value=({"adInfos": None, "totalAdNum": 0}, {})),
            session=SimpleNamespace(read_progress=None),
        )
        self.assertEqual(await read_rows(p, "ad_products", options_for(), 3), ([], 0, True))

    async def test_transient_promotion_login_redirect_waits_for_native_page(self):
        from unittest.mock import patch

        page = promotions.PromotionPage(SimpleNamespace())
        page.call = AsyncMock()
        page.evaluate = AsyncMock(side_effect=[{"login": True, "ready": False}, {"login": False, "ready": True}])
        page.select = AsyncMock(return_value=True)
        with patch("backend.promotions.asyncio.sleep", new=AsyncMock()):
            await page.open_promotion("ad_reports", options_for())
        self.assertEqual(page.evaluate.await_count, 2)
        page.select.assert_awaited_once_with("今日")
