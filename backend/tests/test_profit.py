import tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from backend.center import profit as p, reconciliation as a, report_view as r, promotion_ledger as b


class ProfitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "test.db"))
        for meta in (r.meta, a.meta, b.meta):
            meta.create_all(self.engine)
        self.app = FastAPI()
        p.install(self.app, SimpleNamespace(engine=self.engine), lambda: None)
        self.client = TestClient(self.app)
        self.summary = dict(
            revenue="100",
            product_cost="20",
            shipping="5",
            complete=True,
            revenue_complete=True,
            shipping_complete=True,
            unmatched=0,
            book_name="成本v1",
        )
        with self.engine.begin() as db:
            db.execute(
                r.documents.insert().values(
                    id="a" * 32,
                    shop_id=1,
                    shop_name="店铺",
                    start="2026-09-01 00:00:00",
                    end="2026-10-01 00:00:00",
                    ready=1,
                    created=1,
                )
            )
            db.execute(a.bindings.insert().values(job_id="a" * 32, book_id="c" * 32, updated=1, summary=self.summary))
            db.execute(
                b.bills.insert().values(
                    job_id="b" * 32,
                    shop_id="1",
                    shop_name="店铺",
                    created=1,
                    rows=[
                        ["2026-09", "现金", "0", "0", "1000", "10"],
                        ["2026-09", "红包", "0", "0", "200", "200"],
                        ["2026-08", "现金", "0", "0", "0", "999"],
                    ],
                )
            )

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def get(self):
        return self.client.get("/api/profit?month=2026-09").json()["items"][0]

    def put(self, draft, revision=0):
        return self.client.put("/api/profit/1/2026-09", json=dict(draft=draft, revision=revision))

    def checked(self):
        draft = p.clean(
            dict(actual_shipping="7", evidence="账单2026-09，跨月退款归原订单月份", checks={k: True for k in p.CHECKS})
        )
        saved = self.put(draft).json()
        response = self.client.post(
            "/api/profit/1/2026-09/confirm", json={"revision": saved["revision"], "fingerprint": saved["fingerprint"]}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_cash_only_latest_bill_no_duplicate_and_month_isolated(self):
        self.assertEqual(self.get()["calculation"]["estimated"], "65")
        with self.engine.begin() as db:
            db.execute(
                b.bills.insert().values(
                    job_id="d" * 32,
                    shop_id="1",
                    shop_name="店铺",
                    created=2,
                    rows=[["2026-09", "现金", "0", "0", "1000", "12"]],
                )
            )
        self.assertEqual(self.get()["calculation"]["estimated"], "63")

    def test_adjustments_once_and_actual_shipping_replaces_estimate(self):
        draft = dict(
            actual_shipping="7",
            lines=[
                dict(kind="refund", amount="3", reference="R1"),
                dict(kind="platform_fee", amount="5", reference="F1", included=True),
                dict(kind="other_fee", amount="2", reference="F2"),
                dict(kind="income_add", amount="10", reference="A1"),
                dict(kind="cost_add", amount="4", reference="A1"),
                dict(kind="cost_return", amount="1", reference="R1"),
            ],
        )
        result = self.put(draft)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["calculation"]["estimated"], "65")
        self.assertEqual(result.json()["calculation"]["shipping"], "7")
        self.assertEqual(self.put(draft).status_code, 409)
        draft["lines"].append(dict(kind="platform_fee", amount="2", reference="F2"))
        self.assertEqual(self.put(draft, 1).status_code, 400)

    def test_missing_cost_and_cash_never_zero_or_confirmed(self):
        with self.engine.begin() as db:
            db.execute(a.bindings.update().values(summary={**self.summary, "complete": False, "unmatched": 1}))
            db.execute(b.bills.update().values(rows=[["2026-09", "现金", "0", "0", "0", None]]))
        current = self.get()
        self.assertIsNone(current["calculation"]["estimated"])
        self.assertIsNone(current["calculation"]["product_cost"])
        self.assertIsNone(current["calculation"]["promotion"])
        self.assertEqual(
            self.client.post(
                "/api/profit/1/2026-09/confirm", json=dict(revision=0, fingerprint=current["fingerprint"])
            ).status_code,
            400,
        )

    def test_checked_snapshot_invalidated_by_cost_or_source_change(self):
        current = self.checked()
        self.assertEqual(current["checked_profit"], "63")
        self.assertEqual(current["status"], "checked")
        with self.engine.begin() as db:
            db.execute(a.bindings.update().values(updated=2, summary={**self.summary, "product_cost": "22"}))
        after = self.get()
        self.assertEqual(after["status"], "stale")
        self.assertIsNone(after["checked_profit"])
        self.assertEqual(after["checked"]["calculation"]["estimated"], "63")
        self.assertEqual(
            self.client.post(
                "/api/profit/1/2026-09/confirm",
                json=dict(revision=current["revision"], fingerprint=current["fingerprint"]),
            ).status_code,
            409,
        )

    def test_input_validation_and_auth(self):
        self.assertEqual(self.client.get("/api/profit?month=2026-13").status_code, 400)
        self.assertEqual(self.put(dict(lines=[dict(kind="refund", amount="NaN", reference="x")])).status_code, 400)
        self.assertEqual(self.put(dict(lines=[dict(kind="refund", amount="1", reference="")])).status_code, 400)

        def reject():
            raise HTTPException(401, "unauthorized")

        app = FastAPI()
        p.install(app, SimpleNamespace(engine=self.engine), reject)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/profit?month=2026-09").status_code, 401)

    def test_draft_change_invalidates_confirmation(self):
        current = self.checked()
        draft = current["draft"]
        draft["evidence"] = "新依据"
        changed = self.put(draft, current["revision"])
        self.assertEqual(changed.json()["status"], "stale")


if __name__ == "__main__":
    unittest.main()
