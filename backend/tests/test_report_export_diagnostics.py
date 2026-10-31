import asyncio, base64, json, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend import report_export_adapter as adapter
from backend.report_exports import Worker, router
from backend.tests.test_report_exports import scope
from backend.shop_lock import ShopLock
from backend.task_timing import TaskMeta


class Diagnostics(unittest.IsolatedAsyncioTestCase):
    async def test_new_fields_match_aliases_and_native_ids_without_reusing_old_report(self):
        from backend.report_export_store import FIELDS, LEGACY_FIELDS
        from backend.tests.test_report_exports import scope
        from backend.business_reader import BEIJING
        from datetime import datetime

        start, end = scope()
        job = {"start": start, "end": end, "submitted_at": time.time(), "fields": FIELDS}
        row = {
            "id": "new",
            "groupStartTime": int(datetime.fromisoformat(start).replace(tzinfo=BEIJING).timestamp()),
            "groupEndTime": int(datetime.fromisoformat(end).replace(tzinfo=BEIJING).timestamp()),
            "orderType": 0,
            "afterSaleType": 1,
            "createdTime": time.time(),
            "titles": [1, 2, 3, 13, 24, 25, 12, 30, 21, 22],
        }
        self.assertTrue(adapter.match(row, job))
        self.assertFalse(adapter.match({**row, "titles": [1, 2, 3, 13, 24, 25, 12, 30]}, job))
        self.assertTrue(adapter.match({**row, "titles": [f.replace("商品ID", "商品id") for f in FIELDS]}, job))
        self.assertTrue(adapter.match({**row, "titles": LEGACY_FIELDS}, {**job, "fields": LEGACY_FIELDS}))
        with tempfile.TemporaryDirectory() as folder:
            from backend.report_export_store import Ledger

            ledger = Ledger(folder)
            a = ledger.create({"id": 1}, start, end, fields=LEGACY_FIELDS)
            b = ledger.create({"id": 1}, start, end)
            self.assertNotEqual(a["id"], b["id"])
            self.assertEqual(len(b["fields"]), 10)

    async def test_first_export_guide_and_both_field_button_names(self):
        for name in ("配置字段", "添加报表字段"):
            # The tutorial disappears, then the real field button becomes available.
            page = SimpleNamespace(
                evaluate=AsyncMock(side_effect=[False, False, True, False, False, False, False, True]),
                export_progress=lambda _: None,
            )
            with patch.object(adapter.asyncio, "sleep", AsyncMock()), patch.object(adapter.ui, "state", AsyncMock()):
                await adapter.export_click(page, ["配置字段", "添加报表字段"])
            self.assertEqual(page.evaluate.await_count, 8)
            self.assertIn(name, page.evaluate.await_args_list[-1].args[0])

    async def test_missing_field_button_reports_precise_step(self):
        page = SimpleNamespace(evaluate=AsyncMock(return_value=False))
        with (
            patch.object(adapter.asyncio, "sleep", AsyncMock()),
            self.assertRaisesRegex(ValueError, "添加报表字段.*失败截图"),
        ):
            await adapter.export_click(page, ["配置字段", "添加报表字段"])

    async def test_submission_requires_native_success_response(self):
        for response, expected in (
            ({"success": True, "result": {"id": 42}}, None),
            ({"success": False, "errorMsg": "导出间隔不足"}, adapter.SubmissionRejected),
        ):
            page = SimpleNamespace(evaluate=AsyncMock(return_value=True), export_responses=asyncio.Queue())
            page.export_responses.put_nowait(response)
            with patch.object(adapter, "ready_to_submit", AsyncMock(return_value=True)):
                if expected:
                    with self.assertRaisesRegex(expected, "导出间隔不足"):
                        await adapter.submit(page)
                else:
                    self.assertEqual(await adapter.submit(page), {"id": 42})
            self.assertFalse(page.submission_armed)

    async def test_unavailable_button_is_not_a_submission(self):
        page = SimpleNamespace(evaluate=AsyncMock(return_value=False), export_responses=asyncio.Queue())
        with (
            patch.object(adapter, "ready_to_submit", AsyncMock(return_value=True)),
            self.assertRaisesRegex(adapter.SubmissionRejected, "未执行提交"),
        ):
            await adapter.submit(page)

    async def test_missing_receipt_stays_uncertain(self):
        page = SimpleNamespace(evaluate=AsyncMock(return_value=True), export_responses=asyncio.Queue())

        async def timeout(awaitable, *_):
            awaitable.close()
            raise TimeoutError()

        with (
            patch.object(adapter, "ready_to_submit", AsyncMock(return_value=True)),
            patch.object(adapter.asyncio, "wait_for", timeout),
            self.assertRaisesRegex(adapter.PageReadError, "未收到平台受理"),
        ):
            await adapter.submit(page)

    async def test_failed_tab_captured_before_close_and_rejection_can_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            item = SimpleNamespace(shop={"id": 1, "platform": "pdd"}, state="logged_in", lock=ShopLock())
            worker = Worker(SimpleNamespace(directory=Path(folder)), lambda _: item)
            start, end = scope()
            job = worker.ledger.create(item.shop, start, end)
            page = AsyncMock()
            page.call.return_value = {"data": base64.b64encode(b"\xff\xd8fixture").decode()}
            context = AsyncMock()
            context.__aenter__.return_value = page
            with (
                patch.object(adapter, "OrdersPage", return_value=context),
                patch.object(adapter, "configure", AsyncMock()),
                patch.object(adapter, "ready_to_submit", AsyncMock()),
                patch.object(adapter, "reports", AsyncMock(return_value=[])),
                patch.object(adapter, "submit", AsyncMock(side_effect=adapter.SubmissionRejected("平台限频"))),
            ):
                await worker.process(job)
            saved = worker.ledger.get(job["id"])
            self.assertEqual(saved["state"], "exception")
            self.assertIsNone(saved["submitted_at"])
            self.assertTrue(saved["screenshot"])
            self.assertEqual((worker.ledger.path(job["id"]) / "failure.jpg").read_bytes(), b"\xff\xd8fixture")
            app = FastAPI()
            app.include_router(router(worker, lambda: None))
            client = TestClient(app)
            self.assertEqual(client.get("/api/report-exports/" + job["id"] + "/screenshot").content, b"\xff\xd8fixture")
            self.assertEqual(client.post("/api/report-exports/" + job["id"] + "/retry").json()["state"], "queued")

    async def test_explicit_restart_preserves_previous_intent(self):
        with tempfile.TemporaryDirectory() as folder:
            worker = Worker(SimpleNamespace(directory=Path(folder)), None)
            start, end = scope()
            job = worker.ledger.create({"id": 1}, start, end)
            worker.ledger.update(job["id"], state="exception", submitted_at=123)
            app = FastAPI()
            app.include_router(router(worker, lambda: None))
            client = TestClient(app)
            result = client.post(
                "/api/report-exports/" + job["id"] + "/retry", json={"restart_submission": True}
            ).json()
            self.assertEqual(result["state"], "queued")
            self.assertIsNone(result["submitted_at"])
            self.assertEqual(result["previous_submission"]["submitted_at"], 123)


class StepLogTests(unittest.TestCase):
    def test_stage_changes_emit_once_and_file_does_not_contain_secrets(self):
        from backend.operation_log import step

        with tempfile.TemporaryDirectory() as folder:
            store = SimpleNamespace(directory=Path(folder))
            meta = TaskMeta(stage="before")
            meta.on_stage = lambda text: step(store, {"id": 1, "name": "测试店铺"}, "orders", text)
            meta["stage"] = "打开浏览器 token=secret"
            meta["stage"] = "打开浏览器 token=secret"
            meta["stage"] = "读取第2页"
            text = next((Path(folder) / "launcher-logs").glob("operations-*.log")).read_text(encoding="utf-8")
            self.assertEqual(text.count("打开浏览器"), 1)
            self.assertNotIn("secret", text)
            self.assertIn("读取第2页", text)


class CenterProgressTests(unittest.TestCase):
    def test_old_node_waits_for_update_without_creating_eight_column_job(self):
        from backend.center import report_exports as center
        from backend.center.database import Database
        from backend.report_export_store import LEGACY_FIELDS

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = Database("sqlite:///" + str(root / "center.db"))
            node = db.register("旧版节点")
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
            shop = db.shops()[0]
            ledger = center.CenterLedger(db, root / "report-exports")
            job = ledger.create(shop, *scope())
            ledger.update(job["id"], node=node["id"], local_shop_id=shop["local_id"])

            class Hub:
                peers = {node["id"]: True}
                posts = []

                async def request(self, _, method, path, query="", body=b""):
                    if method == "POST":
                        self.posts.append(path)
                    return {"status": 200, "content": json.dumps({"fields": LEGACY_FIELDS}).encode()}

            app = FastAPI()
            hub = Hub()
            with patch.object(center, "ROOT", root):
                center.install(app, db, hub, lambda: None)
                with TestClient(app) as client:
                    for _ in range(30):
                        result = client.get("/api/report-exports").json()["jobs"][0]
                        if "更新导出字段" in result["message"]:
                            break
                        time.sleep(0.03)
                    self.assertEqual(result["state"], "queued")
                    self.assertIn("更新导出字段", result["message"])
                    self.assertFalse(hub.posts)
            db.engine.dispose()

    def test_live_steps_ignore_node_retry_deadline_and_transfer_failure_screenshot(self):
        from backend.center import report_exports as center
        from backend.center.database import Database

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = Database("sqlite:///" + str(root / "center.db"))
            node = db.register("测试节点")
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
            shop = db.shops()[0]
            start, end = scope()
            ledger = center.CenterLedger(db, root / "report-exports")
            job = ledger.create(shop, start, end)
            ledger.update(
                job["id"],
                node=node["id"],
                local_shop_id=shop["local_id"],
                remote_job_id=job["id"],
                retry_at=time.time() + 300,
            )

            class Hub:
                peers = {node["id"]: True}

                async def request(self, _, method, path, query="", body=b""):
                    content = (
                        b"\xff\xd8fixture"
                        if path.endswith("/screenshot")
                        else json.dumps(
                            {
                                "id": job["id"],
                                "state": "querying",
                                "message": "正在核验订单筛选条件",
                                "retry_at": time.time() + 300,
                                "screenshot": True,
                                "screenshot_at": 123,
                                "updated": time.time(),
                            }
                        ).encode()
                    )
                    return {"status": 200, "content": content}

            app = FastAPI()
            with patch.object(center, "ROOT", root):
                center.install(app, db, Hub(), lambda: None)
                with TestClient(app) as client:
                    for _ in range(30):
                        result = client.get("/api/report-exports").json()["jobs"][0]
                        if result.get("screenshot"):
                            break
                        time.sleep(0.03)
                    self.assertEqual(result["message"], "正在核验订单筛选条件")
                    self.assertTrue(result["screenshot"])
                    self.assertEqual(
                        client.get("/api/report-exports/" + job["id"] + "/screenshot").content, b"\xff\xd8fixture"
                    )
            db.engine.dispose()
