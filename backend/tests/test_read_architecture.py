import asyncio, json, tempfile, time, unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from backend.store import Store
from backend.business import BusinessService, options_for, create_router
from backend import attention, collection, data_versions, table_index, runtime_state
from backend.admission import choose
from backend.query_proxy import local_read


class ReadArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("one", "pw")
        self.other = self.store.add("two", "pw", platform="taobao")
        self.shops = self.store.list()
        self.session = SimpleNamespace(state="logged_in", recovery_status="")
        self.service = BusinessService(self.store, lambda _: self.session)

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, status, started):
        return {
            "rows": [{"id": "x", "status": status, "created_at": "2026-10-10"}],
            "complete": True,
            "_sync": {"snapshot_started_at": started},
        }

    def test_cross_process_revisions_scope_and_atomic_rollback(self):
        peer = Store(self.temp.name)
        q = {"shop_id": str(self.sid)}
        path = "/api/data/overview"
        before = data_versions.revision(peer, path, q)
        self.store.write_cache(self.other, "overview:x", {"rows": [], "complete": True})
        self.store.write_cache(self.sid, "reviews:x", {"rows": [], "complete": True})
        self.assertEqual(data_versions.revision(peer, path, q), before)
        self.store.write_cache(self.sid, "overview:x", {"rows": [], "complete": True})
        version = data_versions.revision(peer, path, q)
        self.assertGreater(version, before)
        with self.assertRaises(ValueError):
            with self.store.connect() as db:
                self.store.invalidate_responses(self.sid, "overview", db=db)
                raise ValueError("rollback")
        self.assertEqual(data_versions.revision(peer, path, q), version)
        self.assertEqual(data_versions.changes(peer, before)["changes"][-1]["shop_id"], self.sid)
        self.store.invalidate_responses()
        self.assertGreater(data_versions.revision(peer, path, q), version)

    def test_focus_updates_filter_sort_without_widening_full_scope(self):
        self.store.write_cache(self.sid, "aftersales:x", self.payload("待商家处理", 100))
        focus = self.payload("平台介入待商家处理", 200)
        focus["rows"].append({"id": "outside", "status": "平台处理中"})
        attention.publish(self.store, self.sid, "aftersales", focus)
        rows = table_index.query(
            self.store, [(self.shops[0], "aftersales:x")], "aftersales", status="平台介入待商家处理"
        )["rows"]
        self.assertEqual([r["id"] for r in rows], ["x"])
        # An older full scan completing later cannot erase an observed newer state.
        self.store.write_cache(self.sid, "aftersales:x", self.payload("已完结", 150))
        self.assertEqual(
            table_index.query(self.store, [(self.shops[0], "aftersales:x")], "aftersales", status="平台介入待商家处理")[
                "total"
            ],
            1,
        )
        self.store.write_cache(self.sid, "aftersales:x", self.payload("已完结", 300))
        attention.reconcile(self.store, self.sid, "aftersales", self.payload("已完结", 300))
        self.assertEqual(attention.read(self.service, shop_id=self.sid, kind="aftersales", summary=True)["total"], 1)
        result = attention.read(self.service, shop_id=self.sid, kind="aftersales", summary=True)
        self.assertFalse(result["rows"])
        self.assertFalse(result["sources"])

    def test_dashboard_new_products_uses_projection_and_correct_months(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        key = self.service.key("products", options_for("products"), self.shops[0])
        self.store.write_cache(
            self.sid,
            key,
            {
                "rows": [
                    {"id": "1", "created_at": "2026-10-10 12:00:00"},
                    {"id": "2", "created_at": "2026-10-09 10:00:00"},
                    {"id": "3"},
                ],
                "complete": True,
            },
        )
        app = FastAPI()
        app.include_router(create_router(self.service, lambda: None))
        with (
            TestClient(app) as c,
            patch.object(self.store, "read_cache", side_effect=AssertionError("dashboard loaded full JSON")),
        ):
            r = c.get(
                "/api/data/products",
                params={
                    "shop_id": self.sid,
                    "dashboard": True,
                    "date_from": "2026-10-10",
                    "date_to": "2026-10-10",
                    "new_products_year": 2026,
                },
            )
            self.assertEqual(r.status_code, 200, r.text)
            d = r.json()
            self.assertEqual(d["total"], 1)
            self.assertEqual(d["previous_total"], 1)
            self.assertEqual(d["monthly"][9]["count"], 2)
            self.assertEqual(d["shops"][0]["unknown_dates"], 1)

    def test_runtime_projection_does_not_contain_credentials_and_expires(self):
        self.service.progress = {(self.sid, "tickets:x"): {"kind": "tickets", "phase": "queued"}}
        runtime_state.save(self.store, self.service, {self.sid: self.session})
        target = BusinessService(self.store, None)
        sessions = {}
        runtime_state.load(self.store, target, sessions)
        self.assertEqual(sessions[self.sid].state, "logged_in")
        self.assertIn((self.sid, "tickets:x"), target.jobs)
        with self.store.connect() as db:
            data = db.execute("SELECT payload FROM collector_runtime").fetchone()[0]
            self.assertNotIn("password", data)
            db.execute("UPDATE collector_runtime SET updated=0")
        runtime_state.load(self.store, target, sessions)
        self.assertFalse(target.jobs)
        self.assertEqual(sessions[self.sid].state, "stopped")

    def test_independent_focus_and_package_cadence(self):
        config = {**collection.DEFAULTS, "module_modes": {"packages": "custom"}, "custom_seconds": 300}
        self.assertEqual(collection.interval_seconds("packages", config), 300)
        self.assertEqual(collection.interval_seconds("aftersales", config), 60)
        opts = dict(attention.scopes(self.shops[0]))["aftersales"]
        self.assertFalse(collection.due("aftersales", opts, 100, config, 159))
        self.assertTrue(collection.due("aftersales", opts, 100, config, 160))
        self.assertFalse(collection.due("packages", {}, 100, config, 399))
        self.assertTrue(collection.due("packages", {}, 100, config, 400))

    def test_backpressure_bounds_queue_without_dropping_due_scopes(self):
        candidates = []
        for sid in range(1, 21):
            for kind, opts in [
                ("aftersales", {"_attention": True}),
                ("tickets", {"_attention": True}),
                ("overview", {}),
                ("products", {}),
            ]:
                candidates.append(({"id": sid, "platform": "pdd"}, None, kind, opts, {"last_success": sid}))
        selected, deferred, caps = choose(candidates, {}, collection.DEFAULTS, 10000)
        self.assertEqual(len(selected) + len(deferred), 80)
        self.assertLessEqual(len(selected), sum(caps.values()))
        self.assertTrue(any(c[3].get("_attention") for c in selected))
        self.assertTrue(any(c[2] == "products" for c in selected))
        self.assertTrue(any(c[2] == "overview" for c in selected))

    def test_query_process_routing_keeps_commands_with_collector(self):
        for path in ["/api/data/overview", "/api/attention", "/api/todos", "/api/events", "/api/product/1/123"]:
            self.assertTrue(local_read("GET", path))
            self.assertFalse(local_read("POST", path))
        for path in ["/api/data/overview/sync", "/api/shops/1/show", "/api/nodes/1/proxy/events"]:
            self.assertFalse(local_read("GET", path))


class QueryIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_retired_query_mode_is_not_a_collector_startup_branch(self):
        from backend import main

        self.assertFalse(hasattr(main, "QUERY_ONLY"))

    async def test_focus_read_uses_reusable_page_and_keeps_session_isolation(self):
        from backend.business_reader import collect
        from contextlib import asynccontextmanager

        session = SimpleNamespace(shop={"platform": "pdd"})
        page = SimpleNamespace(open=AsyncMock())

        @asynccontextmanager
        async def borrow(owner, kind, factory):
            self.assertIs(owner, session)
            self.assertEqual(kind, "aftersales")
            yield page

        with (
            patch("backend.read_page_pool.borrow", borrow),
            patch(
                "backend.operations.collect_operations", new=AsyncMock(return_value={"rows": [], "complete": True})
            ) as read,
        ):
            result = await collect(session, "aftersales", {"_attention": True})
            self.assertTrue(result["complete"])
            self.assertIs(read.call_args.args[0], page)
            page.open.assert_awaited_once_with("aftersales")
