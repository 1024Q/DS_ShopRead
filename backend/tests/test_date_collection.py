import asyncio, json, tempfile, unittest
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.store import Store
from backend.business import BusinessService, options_for, create_router
from backend.date_collection import exact_options
from backend import collection, attention
from backend.operations import collect_operations
from backend.incremental_collection import supported


class DateCollection(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("pdd", "pw")
        self.foreign = self.store.add("taobao", "pw", platform="taobao")
        self.shop = self.store.list()[0]
        self.session = SimpleNamespace(state="stopped", retired=False, control_until=0, recovery_status="")
        self.service = BusinessService(self.store, lambda _: self.session)
        app = FastAPI()
        app.include_router(create_router(self.service, lambda: None))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_range_post_and_get_share_scope_keep_regular_cache_and_do_not_repeat(self):
        opts = exact_options("reviews", options_for("reviews", "2026-09-01", "2026-09-02"), "2026-09-01", "2026-09-02")
        query = f"shop_id={self.sid}&date_from=2026-09-01&date_to=2026-09-02&exact_dates=true"
        self.assertEqual(self.client.post("/api/data/reviews/sync?" + query + "&force=true").status_code, 202)
        key = self.service.key("reviews", opts, self.shop)
        normal = self.service.key("reviews", options_for("reviews"), self.shop)
        self.assertNotEqual(key, normal)
        self.assertEqual(collection.mode("reviews", opts, collection.DEFAULTS), "daily")
        self.store.write_cache(self.sid, normal, {"rows": [{"id": "normal"}], "complete": True})
        self.store.write_cache(
            self.sid, key, {"rows": [{"id": "dated", "created_at": "2026-09-01", "stars": 1}], "complete": True}
        )
        result = self.client.get("/api/data/reviews?" + query).json()
        self.assertEqual([r["id"] for r in result["rows"]], ["dated"])
        result = self.client.get("/api/data/reviews?" + query + "&stars=1").json()
        self.assertEqual(result["total"], 1)
        self.assertEqual(self.store.read_cache(self.sid, normal)["payload"]["rows"][0]["id"], "normal")
        self.assertEqual(self.client.post("/api/data/reviews/sync?" + query).json()["tasks"][0]["cached"], True)

    def test_reject_invalid_dates_and_unimplemented_platforms(self):
        for query in [
            "date_from=2026-09-03&date_to=2026-09-02",
            "date_from=2099-01-01&date_to=2099-01-01",
            "date_from=2025-01-01&date_to=2026-01-01",
            "date_from=2026-09-01",
        ]:
            self.assertEqual(self.client.post("/api/data/reviews/sync?exact_dates=true&" + query).status_code, 400)
        q = "exact_dates=true&date_from=2026-09-01&date_to=2026-09-01"
        self.assertEqual(self.client.post("/api/data/packages/sync?platform=taobao&" + q).status_code, 400)
        self.assertEqual(self.client.post(f"/api/data/packages/sync?shop_id={self.foreign}&" + q).status_code, 400)
        self.assertEqual(self.client.post("/api/data/goodsdata/sync?" + q).status_code, 400)
        tasks = self.client.post("/api/data/packages/sync?" + q).json()["tasks"]
        self.assertEqual([t["shop_id"] for t in tasks], [self.sid])

    async def test_native_date_arguments_and_all_states(self):
        calls = []

        async def page(session, kind, body, *args):
            calls.append(body.copy())
            return {"rows": [], "source_total": 0, "source_loaded": 0, "complete": True}

        for kind in ("aftersales", "evidence", "intercepts", "packages", "invoices"):
            opts = exact_options(kind, options_for(kind, "2026-09-01", "2026-09-02"), "2026-09-01", "2026-09-02")
            with patch("backend.operations.paginate", side_effect=page):
                calls.clear()
                await collect_operations(SimpleNamespace(), kind, opts)
            keys = {
                "aftersales": ("startCreatedTime", "endCreatedTime"),
                "evidence": ("createStartTime", "createEndTime"),
                "intercepts": ("orderTimeStart", "orderTimeEnd"),
                "packages": ("shippingTimeStart", "shippingTimeEnd"),
                "invoices": ("apply_time_start", "apply_time_end"),
            }[kind]
            factor = 1000 if kind == "evidence" else 1
            self.assertTrue(all(b[keys[0]] == opts["start"] * factor for b in calls))
            self.assertTrue(all(b[keys[1]] == opts["end"] * factor + (999 if factor == 1000 else 0) for b in calls))
            if kind == "invoices":
                self.assertEqual([b["invoice_display_status"] for b in calls], [0, 1, 2, 3])
            self.assertFalse(supported(SimpleNamespace(shop=self.shop), kind, opts))

    def test_exact_intercept_range_does_not_union_undated_focus_orders(self):
        opts = exact_options(
            "intercepts", options_for("intercepts", "2026-09-01", "2026-09-02"), "2026-09-01", "2026-09-02"
        )
        key = self.service.key("intercepts", opts, self.shop)
        self.store.write_cache(self.sid, key, {"rows": [], "complete": True})
        attention.publish(
            self.store, self.sid, "intercepts", {"rows": [{"id": "outside", "status": "拦截失败"}], "complete": True}
        )
        r = self.client.get(
            f"/api/data/intercepts?shop_id={self.sid}&date_from=2026-09-01&date_to=2026-09-02&exact_dates=true"
        )
        self.assertEqual(r.json()["total"], 0)

    def test_catalog_dates_keep_current_catalog_key(self):
        opts = exact_options(
            "products", options_for("products", "2026-09-01", "2026-09-02"), "2026-09-01", "2026-09-02"
        )
        self.assertEqual(
            self.service.key("products", opts, self.shop),
            self.service.key("products", options_for("products"), self.shop),
        )
