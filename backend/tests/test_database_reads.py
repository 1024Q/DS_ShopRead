import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from fastapi import FastAPI, HTTPException
from backend.store import Store
from backend.browser import Session
from backend.business import BusinessService, create_router, options_for
from backend.workspace_api import create_router as workspace_router
from backend import collection
from backend.remote_nodes import PATH


class DatabaseReads(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("fixture", "test")
        self.shop = self.store.list()[0]
        self.session = Session(self.shop, self.store)
        self.session.state = "logged_in"
        self.session.window = AsyncMock()
        self.service = BusinessService(self.store, lambda _: self.session)
        app = FastAPI()
        app.include_router(create_router(self.service, lambda: None))
        app.include_router(workspace_router(self.service, self.store, lambda: None))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")

    async def asyncTearDown(self):
        await self.service.close()
        await self.client.aclose()
        self.tmp.cleanup()

    async def test_all_gets_are_read_only_even_with_legacy_force_parameters(self):
        with patch("backend.business.collect", new_callable=AsyncMock) as collect:
            for path in (
                "data/products",
                "data/transactions",
                "data/products?export=true",
                "todos",
                f"product/{self.sid}/123",
            ):
                response = await self.client.get(
                    "/api/" + path + ("&" if "?" in path else "?") + "refresh=true&force=true"
                )
                self.assertEqual(response.status_code, 200, response.text[:100])
            self.assertFalse(self.service.jobs)
            collect.assert_not_awaited()
            with self.store.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM collection_tasks").fetchone()[0], 0)

    async def test_missing_scope_merges_concurrent_commands_and_reads_saved_results(self):
        gate = asyncio.Event()

        async def collect(*args):
            await gate.wait()
            return {"rows": [], "complete": True}

        with patch("backend.business.collect", side_effect=collect) as reader:
            for _ in range(3):
                response = await self.client.post("/api/data/products/sync")
                self.assertEqual(response.status_code, 202)
            self.assertEqual(len(self.service.jobs), 1)
            self.assertTrue((await self.client.get("/api/data/products")).json()["sources"][0]["syncing"])
            gate.set()
            await asyncio.gather(*list(self.service.jobs.values()))
            self.assertEqual(reader.call_count, 1)
            saved = (await self.client.get("/api/data/products")).json()["sources"][0]
            self.assertTrue(saved["complete"])
            self.assertFalse(saved["cache_missing"])
            await self.client.post("/api/data/products/sync")
            self.assertFalse(self.service.jobs)

    async def test_existing_stale_scope_waits_for_scheduler_unless_forced(self):
        options = options_for("products")
        key = self.service.key("products", options, self.shop)
        self.store.write_cache(self.sid, key, {"rows": [], "complete": True})
        with self.store.connect() as db:
            db.execute("UPDATE business_cache SET updated=updated-172800")
        with patch(
            "backend.business.collect", new_callable=AsyncMock, return_value={"rows": [], "complete": True}
        ) as collect:
            await self.client.post("/api/data/products/sync")
            self.assertFalse(self.service.jobs)
            self.assertTrue(collection.state(self.store, self.sid, key)["last_requested"])
            await self.client.post("/api/data/products/sync?force=true")
            await asyncio.gather(*list(self.service.jobs.values()))
            collect.assert_awaited_once()

    async def test_invalid_or_unsupported_commands_never_register(self):
        for path in (
            "data/products/sync?platform=taobao",
            "data/reviews/sync?stars=9",
            "data/reviews/sync?date_from=2026-10-09&date_to=2026-10-01",
        ):
            self.assertEqual((await self.client.post("/api/" + path)).status_code, 400)
        self.assertFalse(self.service.jobs)

    async def test_logged_out_demand_is_persisted_without_browser_access(self):
        self.session.state = "needs_verification"
        self.assertEqual((await self.client.post("/api/data/products/sync")).status_code, 202)
        self.assertFalse(self.service.jobs)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM collection_tasks").fetchone()[0], 1)

    async def test_indexed_api_kinds_paginate_without_full_snapshot_reads(self):
        from backend import goods_data, promotions

        for kind in ("products", "goodsdata", "ad_products", "ad_reports"):
            options = (
                goods_data.options_for("realtime")
                if kind == "goodsdata"
                else promotions.options_for("realtime")
                if kind.startswith("ad_")
                else options_for(kind)
            )
            rows = [
                {"id": str(i), "name": f"商品{i}", "stock": i, "visitors": i, "created_at": "2026-10-09"}
                for i in range(130)
            ]
            self.store.write_cache(
                self.sid, self.service.key(kind, options, self.shop), {"rows": rows, "complete": True}
            )
            with patch.object(self.store, "read_cache", side_effect=AssertionError("full snapshot deserialization")):
                response = await self.client.get(
                    f"/api/data/{kind}?sort_by=stock&sort_direction=desc&page=2&page_size=20"
                )
            self.assertEqual(response.status_code, 200, response.text[:100])
            data = response.json()
            self.assertEqual(data["total"], 130)
            self.assertEqual([r["stock"] for r in data["rows"]], list(range(109, 89, -1)))

    async def test_command_authentication_and_remote_routes(self):
        def deny():
            raise HTTPException(401, "unauthorized")

        app = FastAPI()
        app.include_router(create_router(self.service, deny))
        app.include_router(workspace_router(self.service, self.store, deny))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            for path in ("data/products/sync", "todos/sync", f"product/{self.sid}/123/sync"):
                self.assertTrue(PATH.fullmatch(path))
                self.assertEqual((await client.post("/api/" + path)).status_code, 401)
