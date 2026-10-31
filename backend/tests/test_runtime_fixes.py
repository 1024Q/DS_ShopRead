import asyncio, json, os, sqlite3, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import websockets
from fastapi.testclient import TestClient
from backend import main
from backend.store import Store
from backend.browser import Session, LOGIN_PROBE
from backend.business import BusinessService
from backend.database_maintenance import Maintenance, save as save_policy
from backend.export_jobs import ExportJobs, RETENTION_SECONDS
from backend import system_status


class ApiRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("merchant", "pw")
        self.patches = [
            patch("backend.main.store", self.store),
            patch.object(main.business, "cancel_shop", AsyncMock()),
        ]
        [p.start() for p in self.patches]
        self.client = TestClient(main.app)
        self.headers = {"Authorization": "Bearer " + main.TOKEN}
        self.item = SimpleNamespace(
            lock=asyncio.Lock(),
            retired=False,
            stop=AsyncMock(),
            shop=self.store.list()[0],
            public=lambda: {"id": self.sid},
        )
        main.sessions[self.sid] = self.item

    def tearDown(self):
        main.sessions.pop(self.sid, None)
        [p.stop() for p in self.patches]
        self.temp.cleanup()

    def body(self, **changes):
        return {"username": "merchant", "password": "", "name": "", "browser": "system", "platform": "pdd", **changes}

    def test_non_ascii_token_is_rejected_without_server_error(self):
        self.assertFalse(main.token_matches("Bearer 密钥"))
        self.assertTrue(main.token_matches("Bearer " + main.TOKEN))
        result = self.client.get("/api/shops", headers={"Authorization": "Bearer é".encode("latin-1")})
        self.assertEqual(result.status_code, 401)

    def test_edit_reports_browser_stop_failure_separately(self):
        self.item.stop = AsyncMock(side_effect=RuntimeError("cdp gone"))
        result = self.client.put(f"/api/shops/{self.sid}", headers=self.headers, json=self.body(username="other"))
        self.assertEqual(result.status_code, 503)
        self.assertIn("无法关闭店铺浏览器", result.json()["detail"])
        self.assertEqual(self.store.list()[0]["username"], "merchant")

    def test_edit_reports_real_cause_for_other_failures(self):
        with patch.object(self.store, "update", side_effect=RuntimeError("disk full")):
            result = self.client.put(f"/api/shops/{self.sid}", headers=self.headers, json=self.body(name="新名称"))
        self.assertEqual(result.status_code, 500)
        self.assertIn("disk full", result.json()["detail"])
        self.assertNotIn("无法关闭", result.json()["detail"])
        self.item.stop.assert_not_awaited()
        from backend.log_spool import get as log_spool

        log_spool(self.store).flush(self.store)
        self.assertTrue(any(r["event"] == "edit_failed" for r in self.store.read_logs()))

    def test_update_prepare_respects_remote_control_and_short_lease(self):
        from backend import update_gate

        try:
            self.item.control_until = time.time() + 30
            blockers = self.client.get("/api/system/update-blockers", headers=self.headers).json()["blockers"]
            self.assertEqual(blockers["remote_controls"], 1)
            result = self.client.post("/api/system/update-prepare", headers=self.headers)
            self.assertEqual(result.status_code, 409)
            self.assertEqual(result.json()["detail"]["phase"], "remote_control")
            self.assertFalse(update_gate.active())
            self.item.control_until = 0
            with patch.object(main.business, "running_shops", set()):
                result = self.client.post("/api/system/update-prepare", headers=self.headers)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["expires_in"], 30)
            self.assertTrue(update_gate.active())
        finally:
            update_gate.release()

    def test_delete_runs_and_reports_missing_shop(self):
        result = self.client.delete(f"/api/shops/{self.sid}", headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.store.list(), [])


class LegacyCredentialTests(unittest.TestCase):
    def test_plaintext_file_is_renamed_after_import_and_never_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            path = Path(temp) / "pdd-login.local.txt"
            path.write_text("[店铺 1]\n店铺名称=甲\n账号=user1\n密码=secret\n", encoding="utf-8")
            main.retire_legacy_credentials(store, path)
            self.assertFalse(path.exists())
            moved = Path(temp) / "pdd-login.local.txt.imported"
            self.assertTrue(moved.exists())
            self.assertIn("secret", moved.read_text(encoding="utf-8"))
            self.assertEqual([s["username"] for s in store.list()], ["user1"])
            path.write_text("again", encoding="utf-8")
            main.retire_legacy_credentials(store, path)
            self.assertTrue((Path(temp) / "pdd-login.local.txt.imported.1").exists())
            self.assertTrue(moved.exists())


class SessionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.add("merchant", "pw")
        self.session = Session(self.store.list()[0], self.store)
        self.session.window = AsyncMock()

    async def asyncTearDown(self):
        await self.session.close_cdp()
        self.temp.cleanup()

    async def test_unchanged_detected_name_does_not_write(self):
        self.session.evaluate = AsyncMock(
            return_value={"logged": True, "name": "真实店铺", "verify": False, "failed": False, "signedOut": False}
        )
        with patch.object(self.store, "rename", wraps=self.store.rename) as rename:
            for _ in range(3):
                await self.session.refresh()
        self.assertEqual(rename.call_count, 1)
        self.assertEqual(self.store.list()[0]["name"], "真实店铺")

    def test_login_probe_reads_header_before_full_page_text(self):
        self.assertLess(LOGIN_PROBE.index("textContent.includes"), LOGIN_PROBE.index("document.body.innerText"))

    async def test_persistent_socket_handles_concurrent_commands_and_reconnects(self):
        connections = []

        async def handler(ws):
            connections.append(ws)
            held = []
            async for raw in ws:
                message = json.loads(raw)
                if message["method"] == "Slow":
                    held.append(message)
                    continue
                if message["method"] == "Fail":
                    await ws.send(json.dumps({"id": message["id"], "error": {"message": "bad"}}))
                    continue
                if message["method"] == "Drop":
                    await ws.close()
                    return
                await ws.send(json.dumps({"id": message["id"], "result": {"method": message["method"]}}))
                for old in held:
                    await ws.send(json.dumps({"id": old["id"], "result": {"method": "Slow"}}))
                held.clear()

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            self.session.target = AsyncMock(
                return_value={"id": "page", "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}"}
            )
            slow = asyncio.create_task(self.session.command("Slow"))
            await asyncio.sleep(0.05)
            fast = await self.session.command("Fast")
            self.assertEqual(fast, {"method": "Fast"})
            self.assertEqual(await slow, {"method": "Slow"})
            for _ in range(5):
                await self.session.command("Again")
            self.assertEqual(len(connections), 1)
            self.assertEqual(self.session.target.await_count, 1)
            with self.assertRaises(ValueError):
                await self.session.command("Fail")
            self.assertEqual(await self.session.command("Again"), {"method": "Again"})
            self.assertEqual(len(connections), 1)
            with self.assertRaises(Exception):
                await self.session.command("Drop")
            self.assertEqual(await self.session.command("After"), {"method": "After"})
            self.assertEqual(len(connections), 2)
            self.session.CDP_RECHECK_SECONDS = 0
            await self.session.command("Recheck")
            self.assertEqual(len(connections), 2)
            self.session.target.return_value = {"id": "other", "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}"}
            await self.session.command("Switched")
            self.assertEqual(len(connections), 3)


class StatusRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("merchant", "pw")
        self.session = SimpleNamespace(state="logged_in", retired=False, lock=asyncio.Lock())
        self.service = BusinessService(self.store, lambda _: self.session)

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def test_progress_snapshot_skips_sources_and_matches_summary(self):
        self.store.write_cache(self.sid, "products:test", {"rows": [{"id": "a"}], "complete": True})
        full = system_status.snapshot(self.service)
        light = system_status.progress_snapshot(self.service)
        self.assertNotIn("sources", light)
        self.assertIn("sources", full)
        self.assertEqual(light["summary"], full["summary"])
        self.assertEqual({k: v for k, v in light["diagnostics"].items()}, full["diagnostics"])
        row = next(r for r in full["sources"] if r["kind"] == "products")
        self.assertEqual(row["delivery"]["state"], "pending")
        from backend.center.replication_worker import Delivery

        worker = Delivery(self.store.directory, "business", ["center", "node"])
        worker.commit(worker.collect())
        updated = system_status.snapshot(self.service)
        self.assertEqual(
            next(r for r in updated["sources"] if r["kind"] == "products")["delivery"]["state"], "confirmed"
        )

    async def test_sources_use_their_own_task_timeout(self):
        import time

        now = time.time()
        self.store.write_cache(self.sid, "products:test", {"rows": [{"id": "a"}], "complete": True})
        self.service.progress[(self.sid, "products:test")] = {
            "kind": "products",
            "phase": "running",
            "scope": {},
            "queued_at": now - 20,
            "started_at": now - 5,
            "timeout_seconds": 90,
            "queue": "custom",
        }
        result = system_status.snapshot(self.service)
        row = next(r for r in result["sources"] if r["kind"] == "products")
        self.assertEqual(row["timeout_seconds"], 90)
        self.assertGreaterEqual(row["remaining_seconds"], 83)
        self.assertLessEqual(row["remaining_seconds"], 85)
        idle = next(r for r in result["sources"] if r["kind"] == "overview")
        self.assertIsNone(idle["started_at"])
        self.assertIsNone(idle["remaining_seconds"])

    async def test_source_queues_keep_independent_progress_and_scope(self):
        import time
        from backend import collection

        collection.save_settings(
            self.store,
            {
                "module_assignments": [
                    {"platform": "pdd", "tab": "task", "kind": "orders@recent", "target": "realtime"},
                    {"platform": "pdd", "tab": "task", "kind": "orders@recheck", "target": "custom"},
                    {"platform": "pdd", "tab": "task", "kind": "orders@unfinished", "target": "custom2"},
                ]
            },
        )
        now = time.time()
        from datetime import datetime, timedelta
        from backend.business_reader import BEIJING

        today = datetime.now(BEIJING).date()
        scopes = {
            "today": {"_order_lane": "recent", "date_from": today.isoformat()},
            "yesterday": {"_order_lane": "recheck", "date_from": (today - timedelta(days=1)).isoformat()},
            "unreceived": {"status": "待收货"},
        }
        for lane, phase in [("today", "running"), ("yesterday", "queued"), ("unreceived", "queued")]:
            key = (self.sid, "orders:" + lane)
            self.service.progress[key] = {
                "kind": "orders",
                "scope": scopes[lane],
                "phase": phase,
                "queued_at": now,
                "stage": lane,
            }
        result = system_status.snapshot(self.service)
        rows = [r for r in result["sources"] if r["kind"] == "orders"]
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["queue"] for r in rows}, {"realtime", "custom", "custom2"})
        for row in rows:
            self.assertEqual(row["queues"], [row["queue"]])
            self.assertEqual(row["scope"], scopes[row["sync_stage"]])
        self.assertEqual(sum(r["data_status"] == "running" for r in rows), 1)
        from backend.source_pages import source_page

        filtered = source_page(result, cadence="realtime")
        orders = [r for r in filtered["rows"] if r["kind"] == "orders"]
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["scope"], scopes["today"])

    async def test_sharded_status_does_not_federate_business_payloads(self):
        from unittest.mock import patch
        from backend.shard_connection import FederatedConnection

        self.store.write_cache(self.sid, "products:test", {"rows": [{"id": "a"}], "complete": True})
        self.store.enable_shop_databases()
        original = FederatedConnection.read_all

        def guarded(db, sql, params, touched):
            self.assertNotIn("payload", sql.lower(), "status must route payload reads to one shop")
            return original(db, sql, params, touched)

        with patch.object(FederatedConnection, "read_all", guarded):
            result = system_status.snapshot(self.service)
        row = next(r for r in result["sources"] if r["kind"] == "products")
        self.assertEqual(row["loaded_count"], 1)

    def write_snapshot(self, db, rows, updated, count=None):
        db.execute(
            "INSERT OR REPLACE INTO attention_snapshots(shop_id,kind,payload,updated,started) VALUES(?,?,?,?,?)",
            (self.sid, "tickets", json.dumps({"rows": rows}), updated, updated),
        )
        if count is not None:
            db.execute(
                "INSERT OR REPLACE INTO attention_snapshot_stats(shop_id,kind,row_count) VALUES(?,?,?)",
                (self.sid, "tickets", count),
            )

    async def test_attention_counts_prefer_sidecar_row_count_and_fall_back(self):
        with self.store.connect() as db:
            for name in ("attention_snapshot_stats", "attention_snapshots"):
                db.execute("DROP TABLE IF EXISTS " + name)
            db.execute(
                "CREATE TABLE attention_snapshots (shop_id INTEGER,kind TEXT,payload TEXT NOT NULL,updated REAL NOT NULL,started REAL NOT NULL,PRIMARY KEY(shop_id,kind))"
            )
            self.write_snapshot(db, [{"id": 1}, {"id": 2}], 1)
            self.assertEqual(system_status.attention_counts(self.store, db)[(self.sid, "tickets")]["loaded_count"], 2)
            db.execute(
                "CREATE TABLE attention_snapshot_stats (shop_id INTEGER,kind TEXT,row_count INTEGER,PRIMARY KEY(shop_id,kind))"
            )
            self.write_snapshot(db, [{"id": 1}], 2)
            self.assertEqual(system_status.attention_counts(self.store, db)[(self.sid, "tickets")]["loaded_count"], 1)
            self.write_snapshot(db, [{"id": 1}], 3, count=7)
            self.assertEqual(system_status.attention_counts(self.store, db)[(self.sid, "tickets")]["loaded_count"], 7)
            # An unchanged snapshot version is served from the cache without re-reading.
            db.execute("UPDATE attention_snapshot_stats SET row_count=99")
            self.assertEqual(system_status.attention_counts(self.store, db)[(self.sid, "tickets")]["loaded_count"], 7)


class RetentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.add("merchant", "pw")
        from backend.operations_insights import initialize

        initialize(self.store)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_daily_prune_removes_old_logs_runs_changes(self):
        old = time.time() - 400 * 86400
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO event_logs(created,last_seen,level,module,event,shop_name,message) VALUES(?,?,'info','t','old','','x')",
                (old, old),
            )
            db.execute(
                "INSERT INTO collection_runs(shop_id,kind,finished,seconds,success,error_kind) VALUES(1,?,?,1,1,?)",
                ("a", old, ""),
            )
            db.execute(
                "INSERT INTO collection_runs(shop_id,kind,finished,seconds,success,error_kind) VALUES(1,?,?,1,1,?)",
                ("a", time.time(), ""),
            )
            db.execute(
                "INSERT INTO attention_changes(shop_id,kind,record_id,change,created) VALUES(1,?,?,?,?)",
                ("t", "r", "new", old),
            )
        self.store.log_event("info", "t", "new", "fresh")
        from backend.log_spool import get as log_spool

        log_spool(self.store).flush(self.store)
        stale_file = log_spool(self.store).folder / '2000-01-01-retention.jsonl'
        stale_file.write_text('old file log\n', encoding='utf-8')
        maintenance = Maintenance(self.store)
        result = await maintenance.prune_due()
        self.assertEqual(result['logs']['storage'], 'files')
        self.assertFalse(stale_file.exists())
        self.assertEqual((result["runs"], result["changes"]), (1, 1))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM collection_runs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM event_logs WHERE event='new'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM event_logs WHERE event='old'").fetchone()[0], 1)
        self.assertTrue(any(r["event"] == "new" for r in self.store.read_logs()))
        self.assertIsNone(await maintenance.prune_due())
        listing = maintenance.listing()["retention_preview"]
        self.assertTrue(listing["automatic_deletion"])
        self.assertFalse(listing["automatic"]["caches"])

    async def test_backup_retention_keeps_newest_and_newest_verified(self):
        folder = self.store.directory / "backups"
        folder.mkdir()
        names = [f"backup-2026010{i}-020000-00000{i}" for i in range(1, 6)]
        for i, name in enumerate(names):
            (folder / (name + ".sqlite3")).write_bytes(b"x")
            (folder / (name + ".json")).write_text(json.dumps({"name": name, "verified": i == 0}), encoding="utf-8")
        orphan = folder / "backup-20250101-020000-abcdef.sqlite3"
        orphan.write_bytes(b"x")
        os.utime(orphan, (1, 1))
        user = folder / "my-copy.sqlite3"
        user.write_bytes(b"x")
        removed = Maintenance(self.store).prune_backups(2)
        self.assertEqual(removed, 3)
        self.assertEqual(sorted(p.stem for p in folder.glob("backup-*.json")), [names[0], names[3], names[4]])
        self.assertFalse(orphan.exists())
        self.assertTrue(user.exists())
        self.assertFalse((folder / (names[1] + ".sqlite3")).exists())

    def test_policy_accepts_new_retention_keys(self):
        self.assertEqual(save_policy(self.store, {"run_days": 14, "change_days": 60})["run_days"], 14)
        with self.assertRaises(ValueError):
            save_policy(self.store, {"run_days": 1})


class ExportCleanupTests(unittest.TestCase):
    def test_expired_jobs_and_files_are_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            jobs = ExportJobs(temp)
            jobs.directory.mkdir(parents=True)
            old = time.time() - RETENTION_SECONDS - 10
            jobs.jobs["old"] = {"id": "old", "status": "complete", "created": old, "finished": old}
            (jobs.directory / "old.xlsx").write_bytes(b"x")
            jobs.jobs["new"] = {"id": "new", "status": "complete", "created": time.time(), "finished": time.time()}
            (jobs.directory / "new.xlsx").write_bytes(b"x")
            jobs.jobs["run"] = {"id": "run", "status": "running", "created": old}
            stray = jobs.directory / "stray.xlsx"
            stray.write_bytes(b"x")
            os.utime(stray, (old, old))
            jobs.cleanup()
            self.assertEqual(set(jobs.jobs), {"new", "run"})
            self.assertFalse((jobs.directory / "old.xlsx").exists())
            self.assertFalse(stray.exists())
            self.assertTrue((jobs.directory / "new.xlsx").exists())
            jobs.pool.shutdown()


if __name__ == "__main__":
    unittest.main()
