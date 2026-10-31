import asyncio, io, json, tempfile, time, unittest, zipfile
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.store import Store
from backend.business import BusinessService, options_for, create_router
from backend import table_index, collection, data_versions
from backend.query_batch import read
from backend.source_pages import source_page
from backend.update_events import events


class WorkspaceOptimization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("fixture", "pw")
        self.shop = self.store.list()[0]

    def tearDown(self):
        self.tmp.cleanup()

    def test_projection_preserves_unchanged_rows_and_repairs_columns(self):
        payload = {
            "rows": [
                {"id": "1", "name": "商品一", "status": "在售中"},
                {"id": "2", "name": "商品二", "status": "在售中"},
            ],
            "complete": True,
        }
        self.store.write_cache(self.sid, "products:test", payload)
        with self.store.connect() as db:
            db.execute("CREATE TABLE writes(n)")
            db.execute("CREATE TRIGGER row_updated AFTER UPDATE ON table_rows BEGIN INSERT INTO writes VALUES(1); END")
        self.store.write_cache(self.sid, "products:test", payload)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM writes").fetchone()[0], 0)
        payload["rows"][1]["name"] = "更新商品"
        self.store.write_cache(self.sid, "products:test", payload)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM writes").fetchone()[0], 1)
        payload["rows"].pop()
        self.store.write_cache(self.sid, "products:test", payload)
        page = table_index.query(self.store, [(self.shop, "products:test")], "products", page=99)
        self.assertEqual(page["page"], 1)
        self.assertEqual(len(page["rows"]), 1)

    def test_batched_archive_fallback_and_logs_cursor(self):
        key = "products:archive"
        payload = {"rows": [{"id": "x"}], "complete": True}
        with self.store.connect() as db:
            db.execute("INSERT INTO daily_archive VALUES(?,?,?,?)", (self.sid, key, json.dumps(payload), 100))
        caches, tasks = read(self.store, {self.sid: key}, False)
        self.assertEqual(caches[self.sid, key]["payload"], payload)
        for n in range(5):
            self.store.log_event("error", "products", "fixture", str(n), self.sid)
        from backend.log_spool import get as log_spool

        log_spool(self.store).flush(self.store)
        first = self.store.read_logs(limit=2, module="products")
        last = first[-1]
        from backend.log_spool import get as log_spool

        log_spool(self.store).flush(self.store)
        second = self.store.read_logs(limit=2, module="products", before=last["last_seen"], before_id=last["id"])
        self.assertFalse({r["id"] for r in first} & {r["id"] for r in second})
        self.assertEqual(len(second), 2)

    def test_sources_page_and_retry_scope(self):
        sources = [
            {
                "shop_id": i,
                "kind": "reviews",
                "state": "logged_in",
                "data_status": "error",
                "error": "failed",
                "failed_scopes": [{"cache_key": "reviews:a"}, {"cache_key": "reviews:b"}],
            }
            for i in range(1, 42)
        ]
        page = source_page({"sources": sources}, page=99, size=30)
        self.assertEqual(page["page"], 2)
        self.assertEqual(len(page["rows"]), 11)
        self.assertEqual(page["retry_count"], 82)
        self.assertEqual(source_page({"sources": sources}, shop_id="1,4", retry=True)["total"], 2)

    def test_background_export_keeps_unicode_headers_and_identifiers(self):
        service = BusinessService(self.store, lambda _: SimpleNamespace(state="logged_in", recovery_status=""))
        key = service.key("products", options_for("products"), self.shop)
        self.store.write_cache(
            self.sid,
            key,
            {"rows": [{"id": "12345678901234567890", "name": "中文商品", "status": "在售中"}], "complete": True},
        )
        app = FastAPI()
        app.include_router(create_router(service, lambda: None))
        app.include_router(service.export_jobs.router(lambda: None))
        try:
            with TestClient(app) as client:
                job = client.get("/api/data/products?export=true&background=true").json()
                self.assertIn("id", job)
                for _ in range(100):
                    job = client.get("/api/exports/" + job["id"]).json()
                    if job["status"] in ("complete", "error"):
                        break
                    time.sleep(0.02)
                self.assertEqual(job["status"], "complete", job)
                response = client.get("/api/exports/" + job["id"] + "/download")
                self.assertEqual(response.status_code, 200)
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    text = archive.read("xl/worksheets/sheet1.xml").decode()
                    self.assertIn("商品名称", text)
                    self.assertIn("12345678901234567890", text)
        finally:
            service.export_jobs.pool.shutdown(wait=True)


class StreamResume(unittest.IsolatedAsyncioTestCase):
    async def test_last_event_id_resumes_changes_without_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            before = store.response_revision
            store.invalidate_responses(0, "reviews")

            class Request:
                headers = {"last-event-id": str(before)}

                async def is_disconnected(self):
                    return False

            stream = events(store, Request(), interval=0.01)
            event = await anext(stream)
            await stream.aclose()
            self.assertIn("id: ", event)
            self.assertIn('"reset": false', event)
            self.assertIn("reviews", event)
