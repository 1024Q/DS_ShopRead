from backend import collection
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from backend.store import Store
from backend.shop_import import import_shops, parse
from backend.system_status import snapshot
from backend import main


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_preview_is_read_only_and_does_not_return_password(self):
        result = import_shops(self.store, '账号,密码,店铺名称,浏览器\nfirst," secret,quoted ",一店,项目')
        self.assertEqual(result["ready"], 1)
        self.assertFalse(result["committed"])
        self.assertEqual(self.store.list(), [])
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(result["rows"][0]["browser"], "bundled")

    def test_commit_encrypts_preserves_password_and_skips_duplicates(self):
        old = self.store.add("existing", "old")
        result = import_shops(
            self.store,
            "账号\t密码\t店铺名称\nExisting\tignored\t\nnew\t  Secret-Password  \t新店\nNEW\tignored\t",
            commit=True,
        )
        self.assertEqual((result["imported"], result["skipped"]), (1, 2))
        new = next(s for s in self.store.list() if s["id"] != old)
        self.assertEqual(self.store.credentials(new["id"])["password"], "  Secret-Password  ")
        self.assertEqual(self.store.credentials(old)["password"], "old")
        self.assertNotIn(b"Secret-Password", self.store.path.read_bytes())
        self.assertNotIn("Secret-Password", json.dumps(result))

    def test_invalid_batch_writes_nothing_and_correct_line_reported(self):
        result = import_shops(self.store, "账号,密码\nvalid,pw\nbroken,", commit=True)
        self.assertEqual(result["invalid"], 1)
        self.assertEqual(result["rows"][1]["line"], 3)
        self.assertFalse(result["committed"])
        self.assertEqual(self.store.list(), [])

    def test_limits_and_malformed_csv(self):
        for content in ["", "only-one-column", 'a,"unterminated']:
            if content == "only-one-column":
                self.assertEqual(import_shops(self.store, content)["invalid"], 1)
            else:
                with self.assertRaises(ValueError):
                    parse(content)
        for content in ["\n".join(f"u{i},pw" for i in range(501)), "x" * (1024 * 1024 + 1)]:
            with self.assertRaises(ValueError):
                parse(content)
        self.assertEqual(parse("\ufeff账号,密码\r\na,b")[0]["username"], "a")

    def test_transaction_rolls_back_if_encryption_fails(self):
        encrypt = self.store.cipher.encrypt
        calls = 0

        def fail_second(value):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("encryption failed")
            return encrypt(value)

        with patch.object(self.store.cipher, "encrypt", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                import_shops(self.store, "a,pw\nb,pw", commit=True)
        self.assertEqual(self.store.list(), [])

    def test_concurrent_retry_does_not_create_duplicate_accounts(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: import_shops(self.store, "same,pw", commit=True), range(2)))
        self.assertEqual(sum(r["imported"] for r in results), 1)
        self.assertEqual(len(self.store.list()), 1)

    def test_api_auth_preview_and_commit(self):
        with patch.object(main, "store", self.store):
            client = TestClient(main.app)
            headers = {"Authorization": "Bearer " + main.TOKEN}
            try:
                body = {"content": "账号,密码\napi-test,Private-Test-Password"}
                self.assertEqual(client.post("/api/shops/import", json=body).status_code, 401)
                response = client.post("/api/shops/import", headers=headers, json=body)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.store.list(), [])
                response = client.post("/api/shops/import", headers=headers, json={**body, "commit": True})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["imported"], 1)
                self.assertNotIn("Private-Test-Password", response.text)
                self.assertNotIn("Private-Test-Password", str(self.store.read_logs(limit=100)))
                self.assertEqual(client.get("/api/system/status").status_code, 401)
            finally:
                client.close()


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("one", "pw")
        self.service = SimpleNamespace(
            store=self.store,
            session_getter=lambda _: SimpleNamespace(state="logged_in"),
            errors={},
            progress={},
            jobs={},
            waiting_login=set(),
            queue_config=collection.settings(self.store),
            runtime_limits={},
        )

    def tearDown(self):
        self.temp.cleanup()

    def record(self, rows, kind):
        return next(r for r in rows if r["kind"] == kind)

    def test_incomplete_metadata_entry_recovers_next_poll(self):
        self.store.write_cache(self.sid, "reviews:race", {"rows": [], "complete": True})
        snapshot(self.service)
        self.store._status_metadata = {key: {} for key in self.store._status_metadata}
        snapshot(self.service)
        result = snapshot(self.service)
        self.assertTrue(self.record(result["sources"], "reviews")["updated_at"])

    def test_read_only_metadata_latest_scope_and_no_business_rows(self):
        for period in ["2026-08", "2026-09"]:
            self.store.write_cache(
                self.sid,
                "reconciliation_month:" + period,
                {
                    "rows": [{"sensitive_order": "not-returned"}],
                    "complete": True,
                    "_sync": {"scope": {"period": period}},
                    "source_total": 1,
                },
            )
        result = snapshot(self.service)
        row = self.record(result["sources"], "reconciliation_month")
        self.assertEqual(row["scope"]["period"], "2026-09")
        self.assertEqual(row["loaded_count"], 1)
        self.assertEqual(self.service.jobs, {})
        self.assertNotIn("not-returned", json.dumps(result))
        self.assertEqual(self.record(result["sources"], "products")["data_status"], "missing")

    def test_running_and_failed_queries_override_prior_cache(self):
        self.store.write_cache(self.sid, "reviews:old", {"rows": [], "complete": True})
        key = (self.sid, "reviews:new")
        self.service.errors[key] = (time.time() + 1, "读取失败")
        self.assertEqual(self.record(snapshot(self.service)["sources"], "reviews")["data_status"], "error")
        self.service.progress[key] = {
            "kind": "reviews",
            "scope": {},
            "phase": "running",
            "queued_at": time.time(),
            "started_at": time.time(),
            "loaded": 12,
            "total": 30,
        }
        self.service.jobs[key] = object()
        result = snapshot(self.service)
        row = self.record(result["sources"], "reviews")
        self.assertEqual(row["sync_loaded"], 12)
        self.assertTrue(row["syncing"])
        self.assertEqual(result["summary"]["running"], 1)
        self.assertIsNone(row["error"])
        self.assertEqual(row["previous_error"], "读取失败")

    def test_metadata_reused_until_update_and_deleted_cache_is_evicted(self):
        self.store.write_cache(self.sid, "products:all", {"rows": [{"id": 1}], "complete": True})
        self.assertEqual(self.record(snapshot(self.service)["sources"], "products")["loaded_count"], 1)
        entries = list(self.store._status_metadata.values())
        snapshot(self.service)
        self.assertIs(list(self.store._status_metadata.values())[0], entries[0])
        self.store.write_cache(self.sid, "products:all", {"rows": [{"id": 1}, {"id": 2}], "complete": True})
        self.assertEqual(self.record(snapshot(self.service)["sources"], "products")["loaded_count"], 2)
        with self.store.connect() as db:
            db.execute("DELETE FROM business_cache")
        self.assertEqual(self.record(snapshot(self.service)["sources"], "products")["data_status"], "missing")
        self.assertEqual(self.store._status_metadata, {})
