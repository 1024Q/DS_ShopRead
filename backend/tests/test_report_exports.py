import asyncio
import io
import json
import tempfile
import time
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.report_export_store import Ledger, FIELDS, LEGACY_FIELDS, dates, pack_download
from backend.report_export_adapter import match
from backend.report_exports import Worker, enabled, router
from backend.business_reader import BEIJING
from backend.shop_lock import ShopLock
from backend.center.database import Database
from backend.center import report_exports as center


def scope():
    end = datetime.now(BEIJING).replace(tzinfo=None, microsecond=0) - timedelta(minutes=1)
    return str(end - timedelta(minutes=1)), str(end)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_scope_and_durable_dedup(self):
        start, end = scope()
        ledger = Ledger(self.root)
        job = ledger.create({"id": 1, "name": "测试店铺"}, start, end, "a" * 32)
        ledger.update(job["id"], submitted_at=time.time(), state="generating")
        reopened = Ledger(self.root)
        self.assertEqual(reopened.create({"id": 1}, start, end)["id"], job["id"])
        self.assertTrue(reopened.create({"id": 1}, start, end, "a" * 32)["submitted_at"])
        with self.assertRaises(ValueError):
            reopened.create({"id": 2}, start, end, "a" * 32)
        with self.assertRaises(ValueError):
            dates(start, end + "+08:00")
        with self.assertRaises(ValueError):
            dates("2000-01-01", end)
        with self.assertRaises(ValueError):
            ledger.path("../escape")

    def test_xlsx_kept_and_zip_extracted_without_unsafe_paths(self):
        source, target = self.root / "download", self.root / "report.zip"
        with zipfile.ZipFile(source, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
        pack_download(source, target, "店铺_日期")
        with zipfile.ZipFile(target) as z:
            self.assertEqual(z.namelist(), ["店铺_日期.xlsx"])
        with zipfile.ZipFile(source, "w") as z:
            z.writestr("orders.csv", "订单号\n123")
        pack_download(source, target, "店铺_日期")
        with zipfile.ZipFile(target) as z:
            self.assertIn("店铺_日期.csv", z.namelist())
            self.assertTrue(any(n.startswith("原始文件/") for n in z.namelist()))
        with zipfile.ZipFile(source, "w") as z:
            z.writestr("../orders.csv", "bad")
        with self.assertRaises(ValueError):
            pack_download(source, target, "店铺")
        source.write_bytes(b"<html>login</html>")
        with self.assertRaises(ValueError):
            pack_download(source, target, "店铺")

    def test_remote_report_must_match_all_conditions(self):
        start, end = scope()
        stamp = time.time()
        job = {"start": start, "end": end, "submitted_at": stamp, "before_ids": ["old"], "fields": LEGACY_FIELDS}
        row = {
            "id": "new",
            "groupStartTime": int(datetime.fromisoformat(start).replace(tzinfo=BEIJING).timestamp()),
            "groupEndTime": int(datetime.fromisoformat(end).replace(tzinfo=BEIJING).timestamp()),
            "orderType": 0,
            "afterSaleType": 1,
            "createdTime": stamp,
            "titles": LEGACY_FIELDS,
        }
        self.assertTrue(match(row, job))
        self.assertTrue(match({**row, "titles": [1, 2, 3, 13, 24, 25, 12, 30]}, job))
        self.assertTrue(match({**row, "titles": ["1", "2", "3", "13", "24", "25", "12", "30"]}, job))
        self.assertFalse(match({**row, "titles": [1, 2, 3, 13, 24, 25, 12, 99]}, job))
        for change in (
            {"id": "old"},
            {"orderType": 1},
            {"afterSaleType": 0},
            {"titles": ["订单号"]},
            {"groupEndTime": 0},
            {"createdTime": stamp - 30},
        ):
            self.assertFalse(match({**row, **change}, job))

    def test_all_nodes_open_by_default_without_gray_file(self):
        store = SimpleNamespace(directory=self.root, list=lambda: [{"id": 1, "platform": "pdd"}])
        worker = Worker(store, None)
        app = FastAPI()
        app.include_router(router(worker, lambda: None))
        client = TestClient(app)
        self.assertTrue(enabled(store))
        self.assertEqual(client.post("/api/report-exports", json={}).status_code, 400)
        start, end = scope()
        result = client.post("/api/report-exports", json={"id": "b" * 32, "shop_id": 1, "start": start, "end": end})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(client.put("/api/report-exports/settings", json={"retention_days": 12}).status_code, 200)
        self.assertEqual(json.loads((self.root / "report-export.json").read_text())["retention_days"], 12)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_download_host_and_redirect_boundary(self):
        import httpx
        from backend.report_export_adapter import download

        with tempfile.TemporaryDirectory() as folder:
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"order\n123"))
            )
            with patch("backend.report_export_adapter.httpx.AsyncClient", return_value=client):
                target = Path(folder) / "download"
                await download("https://pfs.yangkeduo.com/export.csv", target)
                self.assertEqual(target.read_bytes(), b"order\n123")
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(302, headers={"location": "https://untrusted.example/report"})
                )
            )
            with (
                patch("backend.report_export_adapter.httpx.AsyncClient", return_value=client),
                self.assertRaisesRegex(ValueError, "下载域名"),
            ):
                await download("https://pfs.yangkeduo.com/export.csv", target)

    async def test_report_navigation_tolerates_document_replacement(self):
        from backend.report_export_adapter import open_reports, REPORT_PAGE
        from backend.read_errors import PageReadError

        page = AsyncMock()
        page.evaluate.side_effect = [
            PageReadError("页面读取失败"),
            {"ready": False, "login": False},
            {"ready": True, "login": False},
        ]
        with patch("backend.report_export_adapter.asyncio.sleep", new_callable=AsyncMock):
            await open_reports(page)
        page.call.assert_awaited_once_with("Page.navigate", {"url": REPORT_PAGE})
        self.assertEqual(page.evaluate.await_count, 3)

    async def test_report_navigation_does_not_hide_login_failure(self):
        from backend.report_export_adapter import open_reports

        page = AsyncMock()
        page.evaluate.return_value = {"login": True, "ready": False}
        with (
            patch("backend.report_export_adapter.asyncio.sleep", new_callable=AsyncMock),
            self.assertRaisesRegex(ValueError, "需要登录"),
        ):
            await open_reports(page)

    async def test_restart_after_submission_only_reconciles(self):
        with tempfile.TemporaryDirectory() as folder:
            item = SimpleNamespace(shop={"id": 1, "platform": "pdd"}, state="logged_in", lock=ShopLock())
            worker = Worker(SimpleNamespace(directory=Path(folder)), lambda sid: item)
            start, end = scope()
            job = worker.ledger.create(item.shop, start, end)
            job = worker.ledger.update(job["id"], state="submitting", submitted_at=time.time(), before_ids=[])
            page = AsyncMock()
            context = AsyncMock()
            context.__aenter__.return_value = page
            with (
                patch("backend.report_exports.adapter.OrdersPage", return_value=context),
                patch("backend.report_exports.adapter.configure", new_callable=AsyncMock) as configure,
                patch("backend.report_exports.adapter.click", new_callable=AsyncMock) as click,
                patch("backend.report_exports.adapter.open_reports", new_callable=AsyncMock),
                patch("backend.report_exports.adapter.reports", new_callable=AsyncMock, return_value=[]),
            ):
                await worker.process(job)
                configure.assert_not_called()
                click.assert_not_called()
                self.assertEqual(worker.ledger.get(job["id"])["state"], "generating")


class CenterTests(unittest.TestCase):
    def test_offline_selection_rejected_before_partial_batch_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = Database("sqlite:///" + str(root / "center.db"))
            online = db.register("在线")
            offline = db.register("离线")
            for node in (online, offline):
                db.ingest(
                    node["id"],
                    [
                        {
                            "shop": "1",
                            "scope": "shop_metadata",
                            "updated": time.time(),
                            "payload": {"name": "店铺", "platform": "pdd"},
                        }
                    ],
                )
            app = FastAPI()
            hub = SimpleNamespace(peers={online["id"]: True})
            with patch.object(center, "ROOT", root):
                center.install(app, db, hub, lambda: None)
                client = TestClient(app)
                found = client.get("/api/report-exports").json()["shops"]
                self.assertEqual(sum(s["online"] for s in found), 1)
                start, end = scope()
                result = client.post(
                    "/api/report-exports",
                    json={"shop_ids": [s["id"] for s in found], "start": start, "end": end, "confirmed": True},
                )
                self.assertEqual(result.status_code, 400)
                self.assertEqual(center.CenterLedger(db, root / "files").all(), [])
            db.engine.dispose()

    def test_all_node_shops_dispatch_to_owner_and_metadata_durable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = Database("sqlite:///" + str(root / "center.db"))
            local = db.register("本机")
            other = db.register("其他机器")
            for node in (local, other):
                db.ingest(
                    node["id"],
                    [
                        {
                            "shop": "1",
                            "scope": "shop_metadata",
                            "updated": time.time(),
                            "payload": {"name": "测试店铺", "platform": "pdd"},
                        }
                    ],
                )
            (root / "report-export.gray.json").write_text(json.dumps({"node": local["id"]}))
            app = FastAPI()
            hub = SimpleNamespace(peers={local["id"]: True, other["id"]: True})
            with patch.object(center, "ROOT", root):
                center.install(app, db, hub, lambda: None)
                client = TestClient(app)
                found = client.get("/api/report-exports").json()["shops"]
                self.assertEqual(len(found), 2)
                self.assertEqual({s["node_id"] for s in found}, {local["id"], other["id"]})
                start, end = scope()
                body = {"shop_ids": [found[0]["id"]], "start": start, "end": end, "confirmed": True}
                first = client.post("/api/report-exports", json=body)
                self.assertEqual(first.status_code, 200, first.text)
                again = client.post("/api/report-exports", json=body)
                self.assertEqual(first.json()["jobs"][0]["id"], again.json()["jobs"][0]["id"])
                foreign = next(s["id"] for s in db.shops() if s["node_id"] == other["id"])
                remote = client.post("/api/report-exports", json={**body, "shop_ids": [foreign]})
                self.assertEqual(remote.status_code, 200, remote.text)
                self.assertEqual(remote.json()["jobs"][0]["node"], other["id"])
                self.assertEqual(len(center.CenterLedger(db, root / "files").all()), 2)
                self.assertEqual(len(client.get("/api/report-exports").json()["jobs"]), 2)
            db.engine.dispose()

    def test_targeted_manifest_does_not_change_other_nodes(self):
        from backend.center import updates

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fake = root / "backend/center/updates.py"
            fake.parent.mkdir(parents=True)
            releases = root / ".data/center/releases"
            releases.mkdir(parents=True)
            (releases / "latest.json").write_text('{"version":"stable"}')
            (releases / "latest-local.json").write_text('{"version":"pilot"}')
            with patch.object(updates, "__file__", str(fake)):
                for node, expected in [("local", "pilot"), ("other", "stable")]:
                    app = FastAPI()
                    updates.install(app, lambda: node)
                    self.assertEqual(TestClient(app).get("/agent/update/latest").json()["version"], expected)

    def test_publisher_only_writes_target_manifest(self):
        import importlib.util
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        spec = importlib.util.spec_from_file_location(
            "report_export_publish", Path(__file__).resolve().parents[2] / "scripts/publish-collector-update.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        node = "af7c71e0-048c-495d-9444-32bbcfb1bc51"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            releases = root / ".data/center/releases"
            releases.mkdir(parents=True)
            stable = releases / "latest.json"
            stable.write_bytes(b'{"release":{"version":"stable"}}')
            with (
                patch.object(module, "ROOT", root),
                patch.object(module, "load_key", return_value=Ed25519PrivateKey.generate()),
                patch.object(module, "dirty_files", return_value=[]),
                patch.object(module, "collect", return_value={"backend/pilot.py": b"pilot"}),
                patch.object(module, "requirement_hashes", return_value=("same", "same")),
            ):
                module.publish(skip_tests=True, node_id=node)
            self.assertEqual(stable.read_bytes(), b'{"release":{"version":"stable"}}')
            self.assertTrue((releases / f"latest-{node}.json").is_file())


if __name__ == "__main__":
    unittest.main()
