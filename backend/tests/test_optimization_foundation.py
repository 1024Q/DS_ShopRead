import asyncio
import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from backend.file_upload import multipart
from backend.schema_migrations import apply
from backend.center.login_guard import LoginGuard
from backend.center import cost_tables
from backend.center.tunnel import Hub, Peer
from backend import recovery_log


def upload(data=b"\x00\xff\r\nPDF", name="report.pdf", fields=None):
    request = httpx.Request("POST", "http://test", files={"file": (name, data)}, data=fields or {})
    return request.headers["content-type"], request.read()


class FoundationTests(unittest.TestCase):
    def test_multipart_binary_and_rejected_duplicate(self):
        content_type, raw = upload()
        self.assertEqual(multipart(content_type, raw, 100), ("report.pdf", b"\x00\xff\r\nPDF", {}))
        with self.assertRaises(ValueError):
            multipart(content_type, raw, 2)
        duplicate = httpx.Request("POST", "http://test", files=[("file", ("a.pdf", b"a")), ("file", ("b.pdf", b"b"))])
        with self.assertRaises(ValueError):
            multipart(duplicate.headers["content-type"], duplicate.read(), 100)
        with self.assertRaises(ValueError):
            multipart(content_type, b"broken", 100)

    def test_cost_multipart_matches_json_and_preserves_sku(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine("sqlite:///" + str(Path(directory) / "test.sqlite3"))
            app = FastAPI()
            cost_tables.install(app, SimpleNamespace(engine=engine), lambda: None)
            data = "商家编码,成本价,快递费\n000SKU,3.25,1.5\n".encode()
            fields = {"sku_column": 0, "cost_column": 1, "shipping_column": 2, "shipping_mode": "piece"}
            try:
                with TestClient(app) as client:
                    legacy = client.post(
                        "/api/cost-tables/preview",
                        json={"filename": "cost.csv", "content": base64.b64encode(data).decode(), **fields},
                    )
                    binary = client.post("/api/cost-tables/preview", files={"file": ("cost.csv", data)}, data=fields)
                    self.assertEqual(binary.status_code, 200, binary.text)
                    self.assertEqual(binary.json(), legacy.json())
                    saved = client.post("/api/cost-tables", files={"file": ("cost.csv", data)}, data=fields)
                    self.assertEqual(saved.status_code, 200, saved.text)
                    self.assertEqual(
                        client.get("/api/cost-tables/" + saved.json()["id"]).json()["rows"][0][0], "000SKU"
                    )
            finally:
                engine.dispose()

    def test_sqlite_failure_rolls_back_and_success_is_idempotent(self):
        db = sqlite3.connect(":memory:")

        def bad(conn):
            conn.execute("CREATE TABLE sample(value TEXT)")
            raise RuntimeError("abort")

        with self.assertRaises(RuntimeError):
            apply(db, "test", [(1, "first", bad)])
        self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='sample'").fetchone())

        def good(conn):
            conn.execute("CREATE TABLE sample(value TEXT)")

        apply(db, "test", [(1, "first", good)])
        db.execute("INSERT INTO sample VALUES('preserved')")
        apply(db, "test", [(1, "first", good)])
        self.assertEqual(db.execute("SELECT value FROM sample").fetchone()[0], "preserved")
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], 1)
        db.close()

    def test_center_schema_baseline_preserves_records(self):
        from backend.center.database import Database

        with tempfile.TemporaryDirectory() as directory:
            url = "sqlite:///" + str(Path(directory) / "center.sqlite3")
            first = Database(url)
            node = first.register("test-node")
            first.engine.dispose()
            second = Database(url)
            try:
                self.assertTrue(second.authenticate(node["id"], node["token"]))
                with second.engine.connect() as db:
                    self.assertEqual(db.execute(text("SELECT count(*) FROM schema_migrations")).scalar(), 1)
            finally:
                second.engine.dispose()

    def test_direct_login_guard_has_bounded_expiring_failures(self):
        clock = [0]
        guard = LoginGuard(clock=lambda: clock[0])
        for _ in range(5):
            with self.assertRaises(HTTPException) as error:
                guard.reject("a")
            self.assertEqual(error.exception.status_code, 401)
        with self.assertRaises(HTTPException) as error:
            guard.reject("a")
        self.assertEqual(error.exception.status_code, 429)
        clock[0] = 901
        with self.assertRaises(HTTPException) as error:
            guard.reject("a")
        self.assertEqual(error.exception.status_code, 401)

    def test_direct_login_guard_lock_is_not_extended_by_retries(self):
        clock = [0]
        guard = LoginGuard(clock=lambda: clock[0])
        for _ in range(5):
            with self.assertRaises(HTTPException):
                guard.reject("a")
        clock[0] = 800
        with self.assertRaises(HTTPException) as error:
            guard.reject("a")
        self.assertEqual(error.exception.status_code, 429)
        clock[0] = 901
        guard.check("a")  # lock ended at 900 despite the retry at 800
        guard.verified("a")
        self.assertNotIn("a", guard.failures)

    def test_center_auth_locks_the_failing_source_only(self):
        from backend.center.database import Database
        from backend.center.server import create_app

        wrong = {"Authorization": "Bearer wrong"}
        valid = {"Authorization": "Bearer " + "t" * 32}
        with tempfile.TemporaryDirectory() as directory:
            database = Database("sqlite:///" + str(Path(directory) / "auth.sqlite3"))
            try:
                app = create_app(database, "t" * 32)
                with TestClient(app, client=("10.0.0.5", 50000)) as attacker:
                    for _ in range(5):
                        self.assertEqual(attacker.get("/admin/nodes", headers=wrong).status_code, 401)
                    self.assertEqual(attacker.get("/admin/nodes", headers=wrong).status_code, 429)
                    # A locked source is refused even with the correct token.
                    self.assertEqual(attacker.get("/admin/nodes", headers=valid).status_code, 429)
                    # A LAN client cannot escape the lock by spoofing the proxy peer header.
                    spoofed = {**valid, "X-ECMP-Request-Peer": "10.0.0.99"}
                    self.assertEqual(attacker.get("/admin/nodes", headers=spoofed).status_code, 429)
                    self.assertEqual(attacker.get("/health").status_code, 200)
                with TestClient(app, client=("10.0.0.6", 50000)) as other:
                    self.assertEqual(other.get("/admin/nodes", headers=valid).status_code, 200)
                with TestClient(app, client=("127.0.0.1", 50000)) as proxy:
                    # The local UI proxy forwards the real browser address.
                    self.assertEqual(
                        proxy.get("/admin/nodes", headers={**valid, "X-ECMP-Request-Peer": "10.0.0.5"}).status_code,
                        429,
                    )
                    self.assertEqual(
                        proxy.get("/admin/nodes", headers={**valid, "X-ECMP-Request-Peer": "10.0.0.7"}).status_code,
                        200,
                    )
                    # Local tooling without a stamped peer is never locked out of valid sessions.
                    for _ in range(6):
                        proxy.get("/admin/nodes", headers=wrong)
                    self.assertEqual(proxy.get("/admin/nodes", headers=valid).status_code, 200)
            finally:
                database.engine.dispose()

    def test_recovery_logs_redact_secrets_and_limit_duplicates(self):
        recovery_log._last.clear()
        with self.assertLogs("ecmp.recovery", level="DEBUG") as logs:
            for _ in range(3):
                try:
                    raise RuntimeError("Bearer SECRET")
                except RuntimeError:
                    recovery_log.suppressed("test-only")
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("SECRET", logs.output[0])
        self.assertIn("RuntimeError", logs.output[0])


class TunnelUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_nodes_receive_binary_and_old_nodes_keep_json(self):
        for capabilities in ({"binary-body", "multipart-upload"}, set()):
            hub = Hub()
            peer = Peer(SimpleNamespace(), capabilities=capabilities)
            hub.peers["node"] = peer
            seen = []

            async def send(message, data=None):
                seen.append((message, data))
                peer.pending[message["id"]].set_result({"status": 200, "content": b"{}"})

            peer.send = send
            content_type, raw = upload()
            await hub.request(
                "node", "POST", "invoice-imports/" + "a" * 32 + "/files", body=raw, content_type=content_type
            )
            message, data = seen[0]
            if capabilities:
                self.assertEqual(message["content_type"], content_type)
                self.assertEqual(data[16:], raw)
            else:
                payload = json.loads(base64.b64decode(message["body"]))
                self.assertEqual(base64.b64decode(payload["content"]), b"\x00\xff\r\nPDF")
                self.assertNotIn("content_type", message)

    async def test_invoice_multipart_reaches_parser_without_base64(self):
        from backend.invoice_import_api import create_router

        app = FastAPI()
        app.include_router(create_router(object(), lambda: None))
        with (
            patch("backend.invoice_import_api.parse", new=AsyncMock(return_value={"parsed": {}})) as parser,
            patch("backend.invoice_import_store.add_file", return_value={"ok": True}) as add,
        ):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                result = await client.post(
                    "/api/invoice-imports/" + "a" * 32 + "/files", files={"file": ("test.pdf", b"%PDF\x00\xff")}
                )
                self.assertEqual(result.status_code, 200, result.text)
                parser.assert_awaited_once_with(b"%PDF\x00\xff")
                self.assertEqual(add.call_args.args[3], b"%PDF\x00\xff")
