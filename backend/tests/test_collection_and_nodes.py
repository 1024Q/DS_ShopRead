import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
import httpx
from backend import collection, remote_nodes
from backend.business import BusinessService, options_for
from backend.business_reader import BEIJING
from backend.browser import Session
from backend.scheduler import Scheduler
from backend.store import Store
from backend.shop_import import import_shops


def stamp(s):
    return datetime.fromisoformat(s).replace(tzinfo=BEIJING).timestamp()


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("one", "pw")

    def tearDown(self):
        self.tmp.cleanup()

    def test_group_interval_is_only_frequency_authority(self):
        from backend.tests.collection_fixture import assign
        from backend.frequency_policy import group

        assign(self.store, {"evidence@attention": "custom2", "products@base": "daily"})
        c = collection.save_settings(
            self.store,
            {
                "settings_patch": {
                    "group_updates": {
                        "custom2": {"name": "巡检", "seconds": 660, "timeout": 123},
                        "daily": {"time": "06:17"},
                    }
                }
            },
        )
        self.assertEqual(collection.interval_seconds("evidence", c, options={"_attention": True}), 660)
        self.assertFalse(collection.due("evidence", {"_attention": True}, 1000, c, 1659))
        self.assertTrue(collection.due("evidence", {"_attention": True}, 1000, c, 1660))
        self.assertEqual(group("evidence", {"_attention": True}, c, "pdd")["timeout"], 123)
        self.assertFalse(collection.due("products", {}, stamp("2026-10-08T08:00:00"), c, stamp("2026-10-09T06:16:59")))
        self.assertTrue(collection.due("products", {}, stamp("2026-10-08T08:00:00"), c, stamp("2026-10-09T06:17:00")))
        self.assertEqual(collection.settings(Store(self.tmp.name)), c)

    def test_task_pause_and_partial_patch_preserve_other_groups(self):
        from backend.tests.collection_fixture import assign

        before = assign(self.store, {"goodsdata@base": "custom3", "reviews@base": "off"})
        c = collection.save_settings(self.store, {"settings_patch": {"group_updates": {"custom2": {"seconds": 421}}}})
        self.assertEqual(c["task_modes"], before["task_modes"])
        self.assertEqual(
            next(g for g in c["frequency_groups"] if g["id"] == "custom3"),
            next(g for g in before["frequency_groups"] if g["id"] == "custom3"),
        )
        self.assertFalse(collection.due("reviews", {}, 0, c))
        self.assertEqual(collection.settings(Store(self.tmp.name)), c)

    def test_obsolete_payloads_and_invalid_updates_are_atomic(self):
        before = collection.settings(self.store)
        for value in (
            {"module_modes": {"reviews": "daily"}},
            {"attention_seconds": 5},
            {"platform_priorities": {}},
            {"settings_patch": {"group_updates": {"custom": {"seconds": 0}}}},
            {"settings_patch": {"max_concurrent": True}},
            {"settings_patch": {"shop_concurrent": 0}},
        ):
            with self.assertRaises(ValueError):
                collection.save_settings(self.store, value)
            self.assertEqual(collection.settings(self.store), before)

    def test_claim_restart_failure_backoff_and_daily_archive(self):
        collection.register(self.store, self.sid, "products:key", "products", {})
        self.assertTrue(collection.claim(self.store, self.sid, "products:key"))
        self.assertFalse(collection.claim(Store(self.tmp.name), self.sid, "products:key"))
        collection.finish(self.store, self.sid, "products:key", error="read failure")
        self.assertFalse(collection.claim(self.store, self.sid, "products:key"))
        self.assertTrue(collection.claim(self.store, self.sid, "products:key", force=True))
        collection.finish(self.store, self.sid, "products:key")
        self.assertFalse(collection.claim(Store(self.tmp.name), self.sid, "products:key"))
        payload = {"rows": [{"id": "saved"}], "complete": True}
        collection.archive(self.store, self.sid, "products:key", "products", {}, payload)
        self.assertEqual(Store(self.tmp.name).read_cache(self.sid, "products:key")["payload"], payload)
        from backend.workspace_api import product_snapshot

        collection.archive(
            self.store,
            self.sid,
            "products:image",
            "products",
            {},
            {"rows": [{"goods_id": "123", "thumbnail_url": "https://example.com/thumb.jpg"}], "complete": True},
        )
        self.assertEqual(
            product_snapshot(Store(self.tmp.name), self.sid, "123")["sections"]["products"]["rows"][0]["goods_id"],
            "123",
        )
        self.assertIn("123", self.store.product_images(self.sid))

    def test_platform_duplicate_scope_import_and_identity_cleanup(self):
        second = self.store.add("one", "pw", platform="taobao")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add("ONE", "pw")
        result = import_shops(self.store, "账号,密码,平台\none,pw,淘宝\none,pw,京东", commit=True)
        self.assertEqual((result["imported"], result["skipped"]), (1, 1))
        collection.register(self.store, second, "products:k", "products", {})
        self.store.update(second, "changed", platform="wechat")
        self.assertIsNone(collection.state(self.store, second, "products:k"))
        self.assertEqual(self.store.credentials(second)["platform"], "wechat")

    def test_legacy_migration_preserves_ids_password_and_sequence(self):
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / "credentials.key"
            key.write_bytes((self.store.directory / "credentials.key").read_bytes())
            db = sqlite3.connect(Path(folder) / "shops.sqlite3")
            db.execute(
                "CREATE TABLE shops(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT DEFAULT '',username TEXT COLLATE NOCASE UNIQUE,password BLOB,browser TEXT DEFAULT 'system')"
            )
            db.execute(
                "INSERT INTO shops(id,username,password) VALUES(7,?,?)", ("old", self.store.cipher.encrypt(b"password"))
            )
            db.execute("INSERT INTO shops(id,username,password) VALUES(30,?,?)", ("deleted", b"x"))
            db.execute("DELETE FROM shops WHERE id=30")
            db.commit()
            db.close()
            migrated = Store(folder)
            self.assertEqual(migrated.list()[0]["id"], 7)
            self.assertEqual(migrated.credentials(7)["password"], "password")
            self.assertGreater(migrated.add("new", "pw"), 30)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("one", "pw")
        self.record = self.store.list()[0]
        self.item = Session(self.record, self.store)
        self.item.state = "logged_in"
        self.item.window = AsyncMock()
        self.item.visible = False
        self.service = BusinessService(self.store, lambda _: self.item)
        self.scheduler = Scheduler(self.service)
        from backend.tests.collection_fixture import assign

        assign(self.store, {"products@base": "daily", "overview@base": "realtime"}, reset=True)

    async def asyncTearDown(self):
        await asyncio.gather(*list(self.scheduler.maintenance.values()), return_exceptions=True)
        await self.service.close()
        self.tmp.cleanup()

    async def drain(self):
        async def finish():
            while True:
                self.service.due_plans.dispatch()
                if self.service.jobs:
                    await asyncio.gather(*list(self.service.jobs.values()))
                elif not any(self.service.due_plans.pending(k) for k in self.service.due_plans.entries):
                    return
                else:await asyncio.sleep(.01)
        await asyncio.wait_for(finish(),10)

    async def test_background_collection_without_page_and_restart_dedup(self):
        async def no_refund_browser(item,store,kind,options,data,**kwargs):return data,False
        # Exercise the scheduler, not merchant refund UI. Homepage summary reads
        # need an explicitly verified summary, not merely complete detail rows.
        with (
            patch("backend.business.collect", new=AsyncMock(return_value={"rows": [], "complete": True,
                  "summary_complete":True,"details_on_demand":True})) as collect,
            patch("backend.auto_refunds.apply",side_effect=no_refund_browser),
        ):
            await self.scheduler.tick()
            await self.scheduler.refill()
            await self.drain()
            self.assertEqual(collect.await_count, 13)
            await self.scheduler.tick()
            await self.scheduler.refill()
            await self.drain()
            self.assertEqual(collect.await_count, 13)  # completion immediately dispatches all remaining due scopes
            restarted = BusinessService(Store(self.tmp.name), lambda _: self.item)
            scheduler = Scheduler(restarted)
            await scheduler.tick()
            await scheduler.refill()
            await asyncio.gather(*list(restarted.jobs.values()))
            await restarted.close()
            await asyncio.sleep(0.05)
            self.assertEqual(collect.await_count, 13)
            with self.store.connect() as db:
                db.execute("UPDATE collection_tasks SET last_success=last_success-70 WHERE kind='overview'")
                db.execute("UPDATE business_cache SET updated=updated-70 WHERE cache_key LIKE 'overview:%'")
            await self.scheduler.tick()
            await self.scheduler.refill()
            await self.drain()
            self.assertEqual(collect.await_count, 14)

    async def test_daily_repeated_ui_queries_use_database(self):
        options = options_for("products")
        with patch("backend.business.collect", new=AsyncMock(return_value={"rows": [], "complete": True})) as collect:
            for _ in range(3):
                self.service.request_sync("products", options)
                await self.drain()
            self.assertEqual(collect.await_count, 1)

    async def test_control_pauses_jobs_and_maintenance_respects_captcha(self):
        self.item.control_until = time.time() + 30
        self.item.command = AsyncMock()
        self.item.refresh = AsyncMock()
        self.scheduler.last_keepalive[self.sid] = 0
        await self.scheduler.tick()
        self.assertFalse(self.service.jobs)
        self.item.command.assert_not_awaited()
        self.item.control_until = 0
        self.item.state = "needs_verification"
        await self.scheduler.tick()
        self.item.command.assert_not_awaited()
        self.item.state = "logged_in"
        self.item.window = AsyncMock()
        config = collection.settings(self.store)
        config["enabled"] = False
        collection.save_settings(self.store, {"settings_patch": {"enabled": config["enabled"]}})
        with patch("backend.scheduler.asyncio.sleep", new=AsyncMock()):
            await self.scheduler.tick()
        self.item.command.assert_awaited_once_with("Page.reload", {"ignoreCache": False})

    async def test_manual_control_cancels_claim_before_collect(self):
        self.item.control_until = time.time() + 20
        with patch("backend.business.collect", new=AsyncMock()) as collect:
            await self.service.sync(self.item, "products", options_for("products"), "products:test")
            collect.assert_not_awaited()


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.calls = []

        def authorize(authorization: str = Header(default="")):
            if authorization != "Bearer primary-test-secret":
                raise HTTPException(401)

        app = FastAPI()
        app.include_router(remote_nodes.create_router(self.store, authorize))
        self.client = TestClient(app)
        self.headers = {"Authorization": "Bearer primary-test-secret"}
        original = httpx.AsyncClient

        async def handle(request):
            self.calls.append(request)
            if request.headers.get("authorization") != "Bearer remote-test-secret":
                return httpx.Response(401)
            if request.url.path.endswith("/screenshot"):
                return httpx.Response(200, content=b"jpeg-fixture", headers={"content-type": "image/jpeg"})
            return httpx.Response(
                200, json=[{"id": 1, "name": "Remote fixture"}] if request.method == "GET" else {"ok": True}
            )

        self.patch = patch(
            "backend.remote_nodes.httpx.AsyncClient",
            side_effect=lambda **kw: original(transport=httpx.MockTransport(handle), **kw),
        )
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.client.close()
        self.tmp.cleanup()

    def add(self):
        return self.client.post(
            "/api/nodes",
            headers=self.headers,
            json={"name": "Machine A", "url": "http://remote.test:8000", "token": "remote-test-secret"},
        )

    def test_auth_encrypted_storage_secret_retained_and_delete(self):
        self.assertEqual(self.client.get("/api/nodes").status_code, 401)
        r = self.add()
        self.assertEqual(r.status_code, 200)
        node = r.json()["id"]
        self.assertNotIn("remote-test-secret", r.text)
        self.assertNotIn(b"remote-test-secret", self.store.path.read_bytes())
        self.assertEqual(
            self.client.put(
                f"/api/nodes/{node}",
                headers=self.headers,
                json={"name": "Renamed", "url": "http://remote.test:8000", "token": ""},
            ).status_code,
            200,
        )
        self.assertEqual(self.calls[-1].headers["authorization"], "Bearer remote-test-secret")
        self.assertEqual(self.client.delete(f"/api/nodes/{node}", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/nodes", headers=self.headers).json(), [])

    def test_proxy_forwards_only_allowed_routes_data_and_inputs(self):
        node = self.add().json()["id"]
        base = f"/api/nodes/{node}/proxy/"
        r = self.client.get(base + "shops/1/screenshot", headers=self.headers)
        self.assertEqual(r.content, b"jpeg-fixture")
        self.assertIn("image/jpeg", r.headers["content-type"])
        body = {"type": "text", "text": "manual-code"}
        self.assertEqual(self.client.post(base + "shops/1/input", headers=self.headers, json=body).status_code, 200)
        self.assertEqual(json.loads(self.calls[-1].content), body)
        r = self.client.get(base + "data/reviews?stars=1,2,3", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.calls[-1].url.params["stars"], "1,2,3")
        self.assertEqual(self.client.get(base + "nodes/3/proxy/shops", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get(base + "arbitrary/admin", headers=self.headers).status_code, 404)

    def test_invalid_connection_does_not_save(self):
        for url in ("file:///etc/test", "http://user:pass@remote.test", "http://remote.test/path"):
            r = self.client.post(
                "/api/nodes", headers=self.headers, json={"name": "Bad", "url": url, "token": "remote-test-secret"}
            )
            self.assertEqual(r.status_code, 400)
        r = self.client.post(
            "/api/nodes",
            headers=self.headers,
            json={"name": "Bad", "url": "http://remote.test:8000", "token": "incorrect-secret"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get("/api/nodes", headers=self.headers).json(), [])
