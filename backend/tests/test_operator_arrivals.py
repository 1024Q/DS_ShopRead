from backend.frequency_policy import selected
import tempfile, unittest
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.store import Store
from backend import main, collection
from backend.business import BusinessService, create_router, options_for
from backend.browser import Session
from backend.new_products import summarize
from backend.scheduler import Scheduler
from backend.tests.test_audit_store import row
from backend.tax_info import options_for as tax_options
from backend.shop_entities import company_names


class OperatorsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.a = self.store.add("one", "pw", operator="甲")
        self.b = self.store.add("two", "pw")

    def test_atomic_bulk_multi_assignment_and_rename(self):
        with patch.object(main, "store", self.store), patch.object(main, "sessions", {}):
            client = TestClient(main.app)
            self.addCleanup(client.close)
            auth = {"Authorization": "Bearer " + main.TOKEN}
            self.assertEqual(
                client.post("/api/operators", headers=auth, json={"names": ["乙", "丙", "乙"]}).status_code, 200
            )
            self.assertEqual(
                client.post(
                    "/api/shops/operators",
                    headers=auth,
                    json={"shop_ids": [self.a, self.b], "operators": ["乙", "丙"], "mode": "append"},
                ).status_code,
                200,
            )
            self.assertEqual(
                client.post(
                    "/api/shops/operators", headers=auth, json={"shop_ids": [self.a, 99999], "operators": ["误分配"]}
                ).status_code,
                400,
            )
            self.assertEqual(
                client.post("/api/operators", headers=auth, json={"name": "新乙", "previous": "乙"}).status_code, 200
            )
        records = {s["id"]: s for s in Store(self.tmp.name).list()}
        self.assertEqual(set(records[self.a]["operators"]), {"甲", "新乙", "丙"})
        self.assertEqual(set(records[self.b]["operators"]), {"新乙", "丙"})
        self.assertEqual(records[self.a]["profile_version"], 0)
        self.assertEqual(self.store.credentials(self.a)["password"], "pw")
        self.store.assign_operator([self.a], "丙", "remove")
        self.assertNotIn("丙", self.store.list()[0]["operators"])
        self.store.update(self.a, "one", name="changed")
        self.assertEqual(set(self.store.list()[0]["operators"]), {"甲", "新乙"})
        self.store.assign_operator([self.a], [])
        self.assertEqual(self.store.list()[0]["operators"], [])
        self.assertEqual(Store(self.tmp.name).list()[0]["operators"], [])

    def test_legacy_migration_preserves_single_operator(self):
        with self.store.connect() as db:
            db.execute("DELETE FROM shop_operators")
            db.execute("DELETE FROM settings WHERE key='multi_operators_v1'")
        self.assertEqual(Store(self.tmp.name).list()[0]["operators"], ["甲"])

    def test_shared_operator_totals_do_not_duplicate_overall(self):
        source = lambda sid, ops, days, ready=True: {
            "shop_id": sid,
            "operators": ops,
            "updated_at": "2026-10-11" if ready else None,
            "complete": True,
            "_creation_counts": {"days": days, "unknown": 0},
        }
        data = {
            "sources": [
                source(1, ["甲", "乙"], {"2026-10-11": 3, "2026-10-10": 2}),
                source(2, ["乙"], {"2026-10-11": 4}),
                source(3, ["丙"], {}, False),
            ],
            "rows": [],
        }
        result = summarize(data, "2026-10-11", 2026, ["甲", "乙", "丙", "丁"])
        operators = {r["name"]: r for r in result["operators"]}
        self.assertEqual(result["total"], 7)
        self.assertEqual(operators["甲"]["count"], 3)
        self.assertEqual(operators["乙"]["count"], 7)
        self.assertEqual(operators["乙"]["months"]["2026-10"], 9)
        self.assertIsNone(operators["丙"]["count"])
        self.assertEqual(operators["丁"]["count"], 0)

    def test_invoice_company_uses_current_profile_and_sorts_before_pagination(self):
        for sid, name in ((self.a, "A企业"), (self.b, "B企业")):
            value = row()
            value["entity"]["company_name"] = name
            self.store.compare_tax(sid, {"rows": [value]}, tax_options("2026-09"))
        service = BusinessService(self.store, lambda _: SimpleNamespace(state="logged_in"))
        for shop in self.store.list():
            key = service.key("invoices", service.query_options("invoices", options_for("invoices"), shop), shop)
            self.store.write_cache(
                shop["id"], key, {"rows": [{"id": str(shop["id"]), "name": "商品", "amount": 1}], "complete": True}
            )
        app = FastAPI()
        app.include_router(create_router(service, lambda: None))
        with TestClient(app) as client:
            data = client.get("/api/data/invoices?sort_by=company_name&sort_direction=desc&page_size=1").json()
        self.assertEqual(data["rows"][0]["company_name"], "B企业")
        self.assertEqual(data["total"], 2)
        with self.store.connect() as db:
            db.execute("UPDATE shops SET profile_version=1 WHERE id=?", (self.a,))
        self.assertNotIn(self.a, company_names(self.store))


class YesterdayScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_pdd_yesterday_is_fixed_daily_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            store.add("one", "pw")
            shop = store.list()[0]
            session = Session(shop, store)
            session.state = "logged_in"
            service = BusinessService(store, lambda _: session)
            scheduler = Scheduler(service)
            from backend.tests.collection_fixture import assign

            config = assign(
                store,
                {
                    "goodsdata@base": "realtime",
                    "transactions@base": "realtime",
                    "goodsdata@yesterday": "history_yesterday",
                },
                reset=True,
            )
            config["keepalive_enabled"] = False
            await scheduler.tick_shop(shop, config)
            scopes = [(kind, opts) for _, _, kind, opts, _ in scheduler.candidates if kind == "goodsdata"]
            self.assertEqual({o["trade_range"] for _, o in scopes}, {"realtime", "yesterday"})
            self.assertEqual(
                selected("goodsdata", next(o for _, o in scopes if o["trade_range"] == "yesterday"), config, "pdd"),
                "history_yesterday",
            )
            self.assertTrue(
                any(
                    k == "transactions" and o.get("trade_range") == "yesterday"
                    for _, _, k, o, _ in scheduler.candidates
                )
            )
            await service.close()
