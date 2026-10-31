import asyncio, json, tempfile, time, unittest, zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from backend.center.database import Database, settings
from backend.center import report_exports as center, report_view
from backend.report_exports import Worker
from backend.report_export_store import Ledger
from backend.tests.test_report_exports import scope


class NodeDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_cancels_active_job_and_worker_continues(self):
        with tempfile.TemporaryDirectory() as folder:
            worker = Worker(SimpleNamespace(directory=Path(folder)), None)
            job = worker.ledger.create({"id": 1}, *scope())
            other = worker.ledger.create({"id": 2}, *scope())
            worker.ledger.update(other["id"], state="exception")
            started = asyncio.Event()
            cancelled = asyncio.Event()

            async def process(job):
                started.set()
                try:
                    await asyncio.sleep(100)
                finally:
                    worker.ledger.update(job["id"], message="取消后最后一次写入")
                    cancelled.set()

            worker.process = process
            loop = asyncio.create_task(worker.run())
            try:
                await asyncio.wait_for(started.wait(), 2)
                await worker.delete(job["id"])
                self.assertTrue(cancelled.is_set())
                self.assertFalse(worker.ledger.path(job["id"]).exists())
                self.assertFalse(loop.done())
                self.assertEqual(worker.ledger.get(other["id"])["shop_id"], 2)
                await worker.delete(job["id"])  # idempotent deferred cleanup
            finally:
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)

    def test_delete_rejects_paths_outside_report_root(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "exports")
            keep = Path(folder) / "keep"
            keep.write_text("safe")
            with self.assertRaises(ValueError):
                ledger.remove("../keep")
            self.assertTrue(keep.exists())


class CenterDeleteTests(unittest.TestCase):
    def test_delete_waits_for_active_import_then_removes_its_rows(self):
        import threading, concurrent.futures

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = Database("sqlite:///" + str(root / "center.db"))
            ledger = center.CenterLedger(db, root / "report-exports")
            job = ledger.create({"id": 1}, *scope())
            path = ledger.path(job["id"]) / "report.zip"
            path.parent.mkdir()
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("report.csv", "订单号,商品\n001,商品甲\n")
            ledger.update(job["id"], state="completed", import_state="pending")
            entered = threading.Event()
            release = threading.Event()
            original = report_view.import_report

            def slow_import(*args):
                entered.set()
                release.wait(4)
                return original(*args)

            app = FastAPI()
            try:
                with patch.object(center, "ROOT", root), patch.object(report_view, "import_report", slow_import):
                    center.install(app, db, SimpleNamespace(peers={}), lambda: None)
                    with TestClient(app) as client, concurrent.futures.ThreadPoolExecutor() as pool:
                        self.assertTrue(entered.wait(2))
                        future = pool.submit(client.delete, "/api/report-exports/" + job["id"])
                        for _ in range(40):
                            if not client.get("/api/report-exports").json()["jobs"]:
                                break
                            time.sleep(0.01)
                        self.assertFalse(future.done())
                        release.set()
                        self.assertEqual(future.result(3).status_code, 200)
                        with db.engine.connect() as con:
                            self.assertEqual(
                                con.execute(select(func.count()).select_from(report_view.rows)).scalar(), 0
                            )
                        self.assertFalse(ledger.path(job["id"]).exists())
                        self.assertFalse(ledger.all())
            finally:
                release.set()
                db.engine.dispose()

    def test_delete_removes_only_matching_database_rows_and_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = Database("sqlite:///" + str(root / "center.db"))
            node = db.register("节点")
            ledger = center.CenterLedger(db, root / "report-exports")
            app = FastAPI()

            class Hub:
                peers = {node["id"]: True}
                calls = []

                async def request(self, node, method, path, query="", body=b""):
                    self.calls.append((method, path))
                    return {"status": 200, "content": b'{"ok":true}'}

            hub = Hub()
            with patch.object(center, "ROOT", root):
                center.install(app, db, hub, lambda: None)
                jobs = []
                for sid in (1, 2):
                    job = ledger.create({"id": sid}, *scope())
                    path = ledger.path(job["id"]) / "report.zip"
                    path.parent.mkdir()
                    with zipfile.ZipFile(path, "w") as archive:
                        archive.writestr("report.csv", "订单号,商品\n001,商品甲\n")
                    report_view.import_report(db.engine, job, path)
                    ledger.update(
                        job["id"], state="completed", import_state="ready", node=node["id"], remote_job_id=job["id"]
                    )
                    jobs.append(job)
                client = TestClient(app)
                response = client.delete("/api/report-exports/" + jobs[0]["id"])
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(response.json()["node_cleanup_pending"])
                self.assertFalse(ledger.path(jobs[0]["id"]).exists())
                self.assertTrue(ledger.path(jobs[1]["id"]).exists())
                with db.engine.connect() as con:
                    self.assertEqual(
                        con.execute(
                            select(func.count())
                            .select_from(report_view.rows)
                            .where(report_view.rows.c.job_id == jobs[0]["id"])
                        ).scalar(),
                        0,
                    )
                    self.assertEqual(
                        con.execute(
                            select(func.count())
                            .select_from(report_view.documents)
                            .where(report_view.documents.c.id == jobs[0]["id"])
                        ).scalar(),
                        0,
                    )
                    self.assertEqual(
                        con.execute(
                            select(func.count())
                            .select_from(report_view.rows)
                            .where(report_view.rows.c.job_id == jobs[1]["id"])
                        ).scalar(),
                        1,
                    )
                self.assertEqual(len(client.get("/api/report-exports").json()["jobs"]), 1)
                self.assertEqual(hub.calls, [("DELETE", "report-exports/" + jobs[0]["id"])])
            db.engine.dispose()

    def test_offline_cleanup_survives_center_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = Database("sqlite:///" + str(root / "center.db"))
            node = db.register("节点")
            ledger = center.CenterLedger(db, root / "report-exports")
            job = ledger.create({"id": 1}, *scope())
            ledger.update(job["id"], node=node["id"], remote_job_id=job["id"])

            class Hub:
                peers = {}
                calls = []

                async def request(self, node, method, path, query="", body=b""):
                    self.calls.append((method, path))
                    return {"status": 200, "content": b'{"ok":true}'}

            hub = Hub()
            with patch.object(center, "ROOT", root):
                app = FastAPI()
                center.install(app, db, hub, lambda: None)
                client = TestClient(app)
                response = client.delete("/api/report-exports/" + job["id"])
                self.assertTrue(response.json()["node_cleanup_pending"])
                self.assertFalse(ledger.all())
                self.assertFalse(hub.calls)
                hub.peers[node["id"]] = True
                again = FastAPI()
                center.install(again, db, hub, lambda: None)
                with TestClient(again):
                    for _ in range(50):
                        with db.engine.connect() as con:
                            pending = con.execute(
                                select(settings.c.key).where(settings.c.key.like("report-export-delete:%"))
                            ).all()
                        if not pending:
                            break
                        time.sleep(0.02)
                    self.assertFalse(pending)
                    self.assertIn(("DELETE", "report-exports/" + job["id"]), hub.calls)
            db.engine.dispose()
