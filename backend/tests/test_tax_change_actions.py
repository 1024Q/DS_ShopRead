import tempfile
import unittest
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.store import Store
from backend import tax_review
from backend.tax_info import options_for
from backend.tests.test_audit_store import row
from backend.workspace_api import create_router


class TaxChangeActionsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(temp.name)
        self.sid = self.store.add("fixture", "password")
        self.opts = options_for("2026-09")
        self.store.compare_tax(self.sid, {"rows": [row()]}, self.opts)
        updated = row(amount=200)
        updated["entity"] = {**updated["entity"], "company_name": "新主体", "credit_code": "CODE2"}
        self.store.compare_tax(self.sid, {"rows": [updated]}, self.opts)
        app = FastAPI()
        app.include_router(create_router(SimpleNamespace(), self.store, lambda: None))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.url = f"/api/tax-review/{self.sid}/2026-09"

    def act(self, data, action):
        return self.client.post(
            self.url,
            json={
                "action": action,
                "revision": data["review"]["revision"],
                "baseline_revision": data["baseline_revision"],
            },
        )

    def test_successful_capture_overwrites_amount_and_entity_without_confirmation(self):
        data = self.client.get(self.url).json()
        self.assertEqual(data["latest"]["tax_amount_cents"], 200)
        self.assertEqual(data["latest"]["entity"]["company_name"], "新主体")
        self.assertEqual(data["baseline"]["tax_amount_cents"], 100)
        self.assertEqual(data["history_total"], 1)
        self.assertFalse(data["can_confirm_entity"])
        self.assertFalse(data["can_overwrite_amounts"])
        for action in ("confirm_entity", "overwrite_amounts"):
            self.assertEqual(self.act(data, action).status_code, 404)
        self.assertEqual(self.client.get(self.url).json()["latest"], data["latest"])

    def test_next_capture_keeps_only_previous_value_and_missing_retains_data(self):
        self.store.compare_tax(self.sid, {"rows": [row(amount=300)]}, self.opts)
        data = self.client.get(self.url).json()
        self.assertEqual(data["latest"]["tax_amount_cents"], 300)
        self.assertEqual(data["baseline"]["tax_amount_cents"], 200)
        self.assertEqual(data["history_total"], 1)
        self.store.compare_tax(self.sid, {"rows": []}, self.opts)
        data = self.client.get(self.url).json()
        self.assertEqual(data["latest"]["tax_amount_cents"], 300)
        self.assertTrue(data["latest"]["_missing_from_source"])

    def test_only_company_name_or_amount_decrease_highlights(self):
        same = row(amount=200)
        same["entity"] = {"company_name": "新主体", "credit_code": "CODE3"}
        self.store.compare_tax(self.sid, {"rows": [same]}, self.opts)
        snapshot = self.store.tax_snapshot(self.sid, self.opts)[0]
        self.assertFalse(snapshot["db_comparison"]["attention"])
        less = {**same, "tax_amount_cents": 150, "tax_amount": 1.5}
        self.store.compare_tax(self.sid, {"rows": [less]}, self.opts)
        self.assertTrue(self.store.tax_snapshot(self.sid, self.opts)[0]["db_comparison"]["attention"])
