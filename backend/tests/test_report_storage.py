import json, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.report_exports import Worker, router
from backend.report_export_store import Ledger
from backend.center import report_exports as center
from backend.center.database import Database
from backend.center.cost_tables import meta as cost_meta
from backend.tests.test_report_exports import scope


class StorageTests(unittest.TestCase):
    def test_center_discovers_original_failed_task_recovery_on_node(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = Database("sqlite:///" + str(root / "center.db"))
            cost_meta.create_all(db.engine)
            node = db.register("节点")
            ledger = center.CenterLedger(db, root / "report-exports")
            job = ledger.create({"id": 1}, *scope())
            ledger.update(
                job["id"], state="exception", message="导出字段勾选核验失败", node=node["id"], remote_job_id=job["id"]
            )

            class Hub:
                peers = {node["id"]: True}

                async def request(self, *args):
                    return {
                        "status": 200,
                        "content": json.dumps(
                            {"state": "queued", "file_bytes": 0, "ui_recovery_revision": "fixed"}
                        ).encode(),
                    }

            try:
                with patch.object(center, "ROOT", root):
                    app = FastAPI()
                    center.install(app, db, Hub(), lambda: None)
                    with TestClient(app) as client:
                        for _ in range(40):
                            result = client.get("/api/report-exports").json()["jobs"][0]
                            if result["state"] == "queued":
                                break
                            time.sleep(0.02)
                        self.assertEqual(result["state"], "queued")
                        self.assertEqual(result["ui_recovery_revision"], "fixed")
            finally:
                db.engine.dispose()

    def test_missing_remote_report_is_not_polled_every_loop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = Database("sqlite:///" + str(root / "center.db"))
            cost_meta.create_all(db.engine)
            node = db.register("节点")
            ledger = center.CenterLedger(db, root / "report-exports")
            job = ledger.create({"id": 1}, *scope())
            job = ledger.update(
                job["id"], state="completed", import_state="ready", node=node["id"], remote_job_id=job["id"]
            )
            stamp = job["updated"]
            path = ledger.path(job["id"])
            path.mkdir()
            (path / "report.zip").write_bytes(b"keep")

            class Hub:
                peers = {node["id"]: True}
                calls = 0

                async def request(self, *args):
                    self.calls += 1
                    return {"status": 404, "content": json.dumps({"detail": "导出任务不存在"}).encode()}

            hub = Hub()
            try:
                with patch.object(center, "ROOT", root):
                    app = FastAPI()
                    center.install(app, db, hub, lambda: None)
                    with TestClient(app) as client:
                        for _ in range(50):
                            result = client.get("/api/report-exports").json()["jobs"][0]
                            if result.get("node_report_missing"):
                                break
                            time.sleep(0.02)
                        time.sleep(2.2)
                        if app.state.report_export_task.done():
                            raise app.state.report_export_task.exception()
                        self.assertEqual(hub.calls, 1)
                        self.assertTrue(result["node_report_missing"])
                        self.assertEqual(result["node_file_bytes"], 0)
                        self.assertEqual(result["state"], "completed")
                        self.assertEqual(result["import_state"], "ready")
                        self.assertEqual(result["updated"], stamp)
                        self.assertEqual((path / "report.zip").read_bytes(), b"keep")
                        self.assertGreater(result["storage_retry_at"], time.time() + 80000)
            finally:
                db.engine.dispose()

    def test_node_reports_all_artifacts_without_counting_state_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            worker = Worker(SimpleNamespace(directory=Path(folder)), None)
            job = worker.ledger.create({"id": 1}, *scope())
            path = worker.ledger.path(job["id"])
            (path / "report.zip").write_bytes(b"zip")
            (path / "original.download").write_bytes(b"original")
            (path / "failure.jpg").write_bytes(b"jpeg")
            app = FastAPI()
            app.include_router(router(worker, lambda: None))
            result = TestClient(app).get("/api/report-exports/" + job["id"]).json()
            self.assertEqual(result["file_bytes"], 15)
            self.assertEqual(worker.ledger.get(job["id"])["state"], "queued")

    def test_center_refreshes_completed_file_sizes_without_extending_retention(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = Database("sqlite:///" + str(root / "center.db"))
            cost_meta.create_all(db.engine)
            node = db.register("节点")
            ledger = center.CenterLedger(db, root / "report-exports")
            job = ledger.create({"id": 1}, *scope())
            stamp = time.time() - 60
            job = ledger.update(
                job["id"], state="completed", import_state="ready", node=node["id"], remote_job_id=job["id"]
            )
            job["updated"] = stamp
            ledger.save(job)
            path = ledger.path(job["id"])
            path.mkdir()
            (path / "report.zip").write_bytes(b"zip")

            class Hub:
                peers = {node["id"]: True}

                async def request(self, *args):
                    return {"status": 200, "content": json.dumps({"file_bytes": 123}).encode()}

            try:
                with patch.object(center, "ROOT", root):
                    app = FastAPI()
                    center.install(app, db, Hub(), lambda: None)
                    with TestClient(app) as client:
                        for _ in range(40):
                            result = client.get("/api/report-exports").json()["jobs"][0]
                            if result.get("node_file_bytes") == 123:
                                break
                            time.sleep(0.02)
                        self.assertEqual(result["center_file_bytes"], 3)
                        self.assertEqual(result["node_file_bytes"], 123)
                        self.assertEqual(result["updated"], stamp)
            finally:
                db.engine.dispose()
