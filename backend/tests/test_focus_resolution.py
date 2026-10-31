import asyncio, json, tempfile, time, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend.store import Store
from backend import attention, table_index, collection, focus_resolution
from backend.business import BusinessService, options_for
from backend.scheduler import Scheduler


class ResolutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("test", "pw")
        self.shop = self.store.list()[0]
        self.item = SimpleNamespace(
            shop=self.shop, state="logged_in", retired=False, visible=False, lock=asyncio.Lock(), control_until=0
        )
        self.service = BusinessService(self.store, lambda _: self.item)
        self.addAsyncCleanup(self.service.close)

    def payload(self, rows, start):
        return {"rows": rows, "complete": True, "_sync": {"snapshot_started_at": start}}

    def prepare(self, kind="aftersales", status="平台处理中"):
        row = {"id": "one", "status": status, "created_at": "2026-10-11", "goods_id": "123", "order_number": "order"}
        for key in (kind + ":a", kind + ":b"):
            self.store.write_cache(self.sid, key, self.payload([row], 100))
        attention.publish(self.store, self.sid, kind, self.payload([row], 200))
        attention.publish(self.store, self.sid, kind, self.payload([], 300))
        return row

    def query(self, key="aftersales:a"):
        return table_index.query(self.store, [(self.shop, key)], key.split(":")[0])

    def test_resolution_clears_all_views_and_retains_unknown_status_until_verified(self):
        self.prepare()
        for key in ("aftersales:a", "aftersales:b"):
            row = self.query(key)["rows"][0]
            self.assertEqual(row["priority"], 0)
            self.assertTrue(row["focus_pending"])
            self.assertEqual(row["status"], "平台处理中")
        self.assertEqual(len(list(focus_resolution.scopes(self.store, self.sid))), 1)
        self.assertEqual(attention.read(self.service, kind="aftersales")["total"], 0)

    def test_verified_normal_status_updates_other_scopes_and_survives_old_cache_replay(self):
        old = self.prepare()
        # A subsequent empty focus heartbeat must not prevent an in-flight full
        # read from confirming a status newer than the original resolution.
        attention.publish(self.store, self.sid, "aftersales", self.payload([], 500))
        closed = {**old, "status": "退款成功", "progress": "已完结"}
        attention.reconcile(self.store, self.sid, "aftersales", self.payload([closed], 400))
        for key in ("aftersales:a", "aftersales:b"):
            row = self.query(key)["rows"][0]
            self.assertEqual(row["status"], "退款成功")
            self.assertFalse(row["focus_pending"])
            self.assertEqual(row["priority"], 0)
        self.store.write_cache(self.sid, "aftersales:a", self.payload([old], 350))
        self.assertEqual(self.query()["rows"][0]["status"], "退款成功")
        self.assertEqual(list(focus_resolution.scopes(self.store, self.sid)), [])
        from backend.workspace_api import product_snapshot

        self.assertEqual(
            product_snapshot(self.store, self.sid, "123")["sections"]["aftersales"]["rows"][0]["status"], "退款成功"
        )

    def test_full_read_resolution_propagates_and_stale_focus_cannot_restore_it(self):
        row = {"id": "one", "status": "待举证"}
        for key in ("evidence:a", "evidence:b"):
            self.store.write_cache(self.sid, key, self.payload([row], 100))
        attention.publish(self.store, self.sid, "evidence", self.payload([row], 200))
        attention.reconcile(self.store, self.sid, "evidence", self.payload([{**row, "status": "已举证"}], 400))
        attention.publish(self.store, self.sid, "evidence", self.payload([row], 350))
        self.assertEqual(self.query("evidence:b")["rows"][0]["status"], "已举证")
        self.assertEqual(attention.read(self.service, kind="evidence")["total"], 0)
        attention.publish(self.store, self.sid, "evidence", self.payload([row], 500))
        self.assertEqual(self.query("evidence:b")["priority_count"], 1)
        self.assertFalse(self.query("evidence:b")["rows"][0]["focus_pending"])

    def test_newer_focus_absence_wins_over_older_broad_positive_status(self):
        old = self.prepare()
        attention.publish(self.store, self.sid, "aftersales", self.payload([], 500))
        attention.reconcile(self.store, self.sid, "aftersales", self.payload([old], 400))
        self.assertEqual(attention.read(self.service, kind="aftersales")["total"], 0)
        self.assertEqual(self.query()["priority_count"], 0)

    def test_quick_filter_resolution_does_not_invent_closed_status(self):
        row = {"id": "p", "status": "发货未揽件", "attention_status": "即将发货-揽收超时"}
        self.store.write_cache(self.sid, "packages:a", self.payload([row], 100))
        attention.publish(self.store, self.sid, "packages", self.payload([row], 200))
        attention.publish(self.store, self.sid, "packages", self.payload([], 300))
        attention.reconcile(self.store, self.sid, "packages", self.payload([{"id": "p", "status": "发货未揽件"}], 400))
        actual = self.query("packages:a")["rows"][0]
        self.assertEqual(actual["status"], "发货未揽件")
        self.assertEqual(actual["priority"], 0)
        self.assertFalse(actual["focus_pending"])

    async def test_scheduler_coalesces_resolution_reads_without_waiting_full_cadence(self):
        self.prepare()
        opts = options_for("aftersales")
        key = self.service.key("aftersales", opts, self.shop)
        collection.register(self.store, self.sid, key, "aftersales", opts, {"updated": time.time()}, False)
        from backend.tests.collection_fixture import assign

        config = assign(self.store, {"aftersales@base": "custom"})
        config["keepalive_enabled"] = False
        scheduler = Scheduler(self.service)
        await scheduler.tick_shop(self.shop, config)
        candidates = [c for c in scheduler.candidates if c[4].get("resolution_due")]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][2], "aftersales")
        focus_resolution.requested(self.store, self.sid, "aftersales")
        self.assertEqual(list(focus_resolution.scopes(self.store, self.sid)), [])
        with self.store.connect() as db:
            db.execute("UPDATE focus_resolutions SET requested=requested-301")
        self.assertEqual(len(list(focus_resolution.scopes(self.store, self.sid))), 1)

    def test_existing_history_repair_runs_once(self):
        self.prepare()
        with self.store.connect() as db:
            db.execute("DELETE FROM focus_resolutions")
            db.execute("DELETE FROM settings WHERE key='focus_resolution_repair_v1'")
        focus_resolution.repair(self.store)
        self.assertTrue(self.query()["rows"][0]["focus_pending"])
        with self.store.connect() as db:
            first = db.execute("SELECT started FROM focus_resolutions").fetchone()[0]
        focus_resolution.repair(self.store)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT started FROM focus_resolutions").fetchone()[0], first)
