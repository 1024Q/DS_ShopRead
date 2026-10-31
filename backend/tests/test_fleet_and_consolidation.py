import asyncio, hashlib, json, tempfile, time, unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
import httpx
from backend.store import Store
from backend.business import BusinessService, options_for
from backend.business_reader import BEIJING
from backend.remote_nodes import initialize as initialize_nodes
from backend.fleet import Fleet, export
from backend import collection, home_progress, operations_insights as ops, attention


class FleetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("account-test", "secret-test")
        initialize_nodes(self.store)
        self.item = SimpleNamespace(state="logged_in", recovery_status="")
        self.service = BusinessService(self.store, lambda _: self.item)
        self.fleet = Fleet(self.service)
        self.shop = self.store.list()[0]
        key = self.service.key("overview", options_for("overview"), self.shop)
        self.store.write_cache(self.sid, key, {"metrics": {"sales": 12, "orders": 2}, "complete": True})

    async def asyncTearDown(self):
        self.temp.cleanup()

    def node(self, n):
        url = f"http://machine{n}:8000"
        token = "fixture-token-only"
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO remote_nodes(id,name,url,token) VALUES(?,?,?,?)",
                (n, f"机器{n}", url, self.store.cipher.encrypt(token.encode())),
            )
        return hashlib.sha256((url + token).encode()).hexdigest()

    def snapshot(self, machine="remote", account="remote-account", sales=30):
        return {
            "version": 1,
            "machine": machine,
            "collector_online": True,
            "date": datetime.now(BEIJING).date().isoformat(),
            "rows": [
                {
                    "shop_id": 1,
                    "name": "同名店",
                    "platform": "pdd",
                    "operator": "张三",
                    "account_key": account,
                    "metrics": {"sales": sales},
                    "updated_at": datetime.now(BEIJING).isoformat(),
                    "supported": True,
                    "complete": True,
                    "state": "logged_in",
                }
            ],
        }

    async def test_database_only_read_offline_and_identity(self):
        self.fleet.save(0, export(self.service))
        fp = self.node(1)
        self.fleet.save(1, self.snapshot(), fingerprint=fp)
        data = self.fleet.read()
        self.assertEqual(data["totals"]["sales"], 42)
        self.assertEqual(len({r["id"] for r in data["rows"]}), 2)
        self.assertEqual(self.fleet.read(operator="张三")["totals"]["sales"], 30)
        self.fleet.save(1, None, "offline", fp)
        data = self.fleet.read()
        self.assertEqual(data["totals"]["sales"], 12)
        self.assertEqual(len(data["rows"]), 2)
        self.assertFalse(data["machines"][1]["online"])
        self.assertEqual(self.service.jobs, {})

    async def test_duplicate_machine_account_and_connection_change(self):
        self.fleet.save(0, export(self.service))
        local = self.fleet.read()["rows"][0]
        fp = self.node(1)
        self.fleet.save(1, self.snapshot(account=local["account_key"]), fingerprint=fp)
        self.assertEqual(self.fleet.read()["totals"]["sales"], 30)
        self.assertEqual(self.fleet.read()["unique_shops"], 1)
        with self.store.connect() as db:
            db.execute("UPDATE remote_nodes SET url='http://changed:8000' WHERE id=1")
        self.assertEqual(len(self.fleet.read()["rows"]), 1)
        fp2 = self.node(2)
        self.fleet.save(2, export(self.service), fingerprint=fp2)
        self.assertTrue(self.fleet.read()["machines"][2]["duplicate"])

    async def test_remote_failure_isolated_and_no_secrets_exported(self):
        self.node(1)
        self.node(2)

        def handler(request):
            if request.url.host == "machine1":
                return httpx.Response(200, json=self.snapshot())
            return httpx.Response(404)

        client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with patch("backend.fleet.httpx.AsyncClient", side_effect=lambda **kw: client(transport=transport, **kw)):
            await self.fleet.tick()
        data = self.fleet.read()
        self.assertEqual(data["totals"]["sales"], 42)
        self.assertIn("未支持", data["machines"][2]["error"])
        text = json.dumps(export(self.service))
        self.assertNotIn("secret-test", text)
        self.assertNotIn("account-test", text)
        self.assertNotIn("username", text)

    async def test_platform_isolation_and_configured_attention_cadence(self):
        from backend.tests.collection_fixture import assign

        assign(self.store, {"packages@base": "custom", "packages@attention": "custom2", "goodsdata@base": "realtime"})
        cfg = assign(self.store, {"goodsdata@base": "daily"}, platform="taobao")
        self.assertEqual(collection.mode("goodsdata", {}, cfg, "taobao"), "daily")
        self.assertEqual(collection.mode("goodsdata", {}, cfg, "pdd"), "realtime")
        self.assertEqual(collection.effective_interval("packages", {}, cfg, "pdd"), 300)
        self.assertFalse(collection.due("packages", {"_attention": True}, 100, cfg, 699))
        self.assertTrue(collection.due("packages", {"_attention": True}, 100, cfg, 700))

    async def test_progress_excludes_stopped_and_separates_daily(self):
        r = home_progress.read(self.service)
        self.assertEqual(r["total_modules"], 12)
        self.assertEqual(r["daily_total"], 1)
        self.item.state = "stopped"
        r = home_progress.read(self.service)
        self.assertEqual(r["total_modules"], 0)
        self.assertEqual(r["excluded_shops"], 1)

    async def test_comparable_history_retains_zero_and_uses_same_cohort(self):
        sid2 = self.store.add("incomplete", "pw")
        today = datetime.now(BEIJING).date()
        for i in range(1, 15):
            stamp = datetime.combine(today - timedelta(days=i), datetime.min.time(), tzinfo=BEIJING).timestamp()
            self.store.write_cache(
                self.sid,
                "overview:fixture",
                {"complete": True, "metrics": {"sales": 0 if i <= 7 else 5}, "_sync": {"snapshot_started_at": stamp}},
            )
        r = ops.history(self.store, comparison="comparable")
        self.assertEqual(r["eligible_shops"], 2)
        self.assertEqual(r["participating_shops"], 1)
        self.assertEqual(r["change_percent"], -100)
        self.assertIsNone(ops.history(self.store, comparison="strict")["total"])

    async def test_notification_history_keeps_resolved_order_reference(self):
        attention.publish(
            self.store,
            self.sid,
            "evidence",
            {"complete": True, "rows": [{"id": "record1", "order_number": "order1", "status": "待举证"}]},
        )
        attention.publish(self.store, self.sid, "evidence", {"complete": True, "rows": []})
        r = ops.notification_history(self.store, change="resolved")
        self.assertEqual(r["rows"][0]["context"]["order_number"], "order1")
