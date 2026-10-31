import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend.center.database import Database
from backend.center.server import create_app
from backend.collector_enrollment import configuration, code


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database("sqlite:///" + str(Path(self.temp.name) / "center.db"))
        self.report_root = patch("backend.center.report_exports.ROOT", Path(self.temp.name) / "reports")
        self.report_root.start()
        self.addCleanup(self.report_root.stop)
        self.client = TestClient(create_app(self.db, "x" * 40))
        self.admin = {"Authorization": "Bearer " + "x" * 40}
        self.token = secrets.token_urlsafe(32)
        self.headers = {"Authorization": "Bearer " + self.token}

    def tearDown(self):
        self.db.engine.dispose()
        self.temp.cleanup()

    def enroll(self):
        return self.client.post("/agent/enroll", headers=self.headers, json={"name": "TEST-PC", "version": "test"})

    def test_pending_survives_restart_and_has_no_node_access(self):
        one = self.enroll()
        self.assertEqual(one.status_code, 200)
        ident = one.json()["id"]
        self.assertEqual(one.json()["state"], "pending")
        self.assertNotIn(self.token, one.text)
        self.client = TestClient(create_app(self.db, "x" * 40))
        self.assertEqual(self.enroll().json()["id"], ident)
        self.assertFalse(self.db.authenticate(ident, self.token))
        self.assertEqual(self.db.registry(), [])
        self.assertEqual(self.client.get("/agent/identify", headers=self.headers).status_code, 401)
        self.assertEqual(
            self.client.post("/agent/claim", headers={**self.headers, "X-Node-ID": ident}).status_code, 401
        )
        self.assertEqual(self.client.post("/admin/enrollments/" + ident, json={"action": "approve"}).status_code, 401)
        rows = self.client.get("/admin/enrollments", headers=self.admin).json()
        self.assertEqual(rows[0]["code"], code({"token": self.token}))
        self.assertNotIn("token_hash", rows[0])

    def test_only_admin_approval_activates_once_and_revocation_sticks(self):
        ident = self.enroll().json()["id"]
        for _ in range(2):
            self.assertEqual(
                self.client.post(
                    "/admin/enrollments/" + ident, headers=self.admin, json={"action": "approve"}
                ).status_code,
                200,
            )
        self.assertTrue(self.db.authenticate(ident, self.token))
        self.assertEqual(len(self.db.registry()), 1)
        self.assertEqual(self.enroll().json()["state"], "approved")
        self.assertEqual(self.client.delete("/admin/nodes/" + ident, headers=self.admin).status_code, 200)
        self.assertEqual(self.enroll().json()["state"], "revoked")
        self.assertFalse(self.db.authenticate(ident, self.token))

    def test_reject_never_creates_node_and_rate_limits_new_requests(self):
        ident = self.enroll().json()["id"]
        self.client.post("/admin/enrollments/" + ident, headers=self.admin, json={"action": "reject"})
        self.assertEqual(self.enroll().json()["state"], "rejected")
        self.assertEqual(self.db.registry(), [])
        self.assertEqual(
            self.client.post("/admin/enrollments/" + ident, headers=self.admin, json={"action": "approve"}).status_code,
            409,
        )
        for _ in range(9):
            self.headers = {"Authorization": "Bearer " + secrets.token_urlsafe(32)}
            self.assertEqual(self.enroll().status_code, 200)
        self.headers = {"Authorization": "Bearer " + secrets.token_urlsafe(32)}
        self.assertEqual(self.enroll().status_code, 429)

    def test_client_preserves_existing_identity_and_generates_unique_new_secret(self):
        a = configuration("http://center:8010", None)
        b = configuration("http://center:8010", None)
        self.assertNotEqual(a["token"], b["token"])
        self.assertEqual(len(a["token"]), 43)
        self.assertEqual(configuration(a["url"], a)["token"], a["token"])
        old = {"url": a["url"], "id": "known", "token": "old-key", "name": "old"}
        self.assertEqual(configuration(old["url"], old)["id"], "known")
        self.assertNotEqual(configuration("http://other:8010", old)["token"], old["token"])


if __name__ == "__main__":
    unittest.main()
