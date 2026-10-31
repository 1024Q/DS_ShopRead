import unittest
from decimal import Decimal
from sqlalchemy import select
from backend.tests.test_profit import ProfitTests
from backend.center import (
    profit_tax as tax,
    profit,
    report_view as report,
    reconciliation as cost,
    table_tools as table,
    database,
)


class TaxTests(ProfitTests):
    def save_tax(self, shop=1, revision=0, apply=True, **changes):
        config = dict(
            rate="3", base="net_receipts", included="0", included_in="revenue", actual=None, reference="", evidence=""
        )
        config.update(changes)
        return self.client.put(
            f"/api/profit-tax/{shop}/2026-09", json=dict(revision=revision, config=config, apply_existing=apply)
        )

    def test_history_pinned_and_explicit_rebind(self):
        first = self.save_tax()
        self.assertEqual(first.status_code, 200, first.text)
        value = self.get()
        self.assertEqual(value["calculation"]["after_tax"], "62.00")
        second = self.save_tax(revision=1, apply=False, rate="5")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.get()["calculation"]["tax_version"], first.json()["id"])
        third = self.save_tax(revision=2, rate="5")
        self.assertEqual(third.status_code, 200)
        self.assertEqual(self.get()["calculation"]["after_tax"], "60.00")
        self.assertEqual(len(self.client.get("/api/profit-tax?month=2026-09").json()["history"]), 3)
        self.assertEqual(self.save_tax(revision=1).status_code, 409)

    def test_refunds_actual_zero_and_no_double_fee(self):
        self.put(
            dict(
                lines=[
                    dict(kind="refund", amount="10", reference="r"),
                    dict(kind="refund", amount="20", reference="r2", included=True),
                ]
            )
        )
        self.assertEqual(self.save_tax().status_code, 200)
        self.assertEqual(self.get()["calculation"]["tax"]["base"], "90")
        self.assertEqual(self.get()["calculation"]["tax_estimated"], "2.70")
        result = self.save_tax(revision=1, actual="4", included="1", reference="T1", evidence="已在实收扣1")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.get()["calculation"]["tax_deduction"], "3")
        self.assertEqual(self.get()["calculation"]["after_tax"], "52")
        self.save_tax(revision=2, actual="0", reference="ZERO", evidence="零税额核对")
        self.assertEqual(self.get()["calculation"]["tax_deduction"], "0")
        self.put(dict(lines=[dict(kind="other_fee", amount="2", reference="T1")]), 1)
        self.save_tax(revision=3, actual="4", included="2", included_in="fees", reference="T1", evidence="已记费用")
        self.assertEqual(self.get()["calculation"]["after_tax"], "61")
        self.save_tax(revision=4, actual="4", reference="T1", evidence="重复费用")
        self.assertIsNone(self.get()["calculation"]["after_tax"])

    def test_default_shop_override_and_month_isolation(self):
        self.save_tax(shop=0, rate="1")
        self.save_tax(rate="3")
        self.save_tax(shop=0, revision=1, rate="9")
        self.assertEqual(self.get()["calculation"]["tax"]["rate"], "3")
        self.assertEqual(self.client.get("/api/profit-tax?month=2026-08").json()["items"], [])
        self.assertEqual(self.save_tax(shop=0, revision=2, actual="3", reference="x", evidence="x").status_code, 400)

    def test_invalid_and_unconfigured_not_zero(self):
        self.assertIsNone(self.get()["calculation"]["after_tax"])
        for rate in ("NaN", "-1", "100.01", "0.1234567", None):
            self.assertEqual(self.save_tax(rate=rate).status_code, 400)
        self.assertEqual(self.save_tax(actual="2").status_code, 400)
        self.save_tax(actual="1", included="2", reference="x", evidence="x")
        self.assertIsNone(self.get()["calculation"]["after_tax"])

    def test_confirmation_invalidated_by_tax_change(self):
        self.save_tax(actual="3", reference="tax", evidence="税款凭据")
        checked = self.checked()
        self.assertEqual(checked["checked_tax_profit"], "60")
        self.save_tax(revision=1, actual="4", reference="tax2", evidence="更正")
        current = self.get()
        self.assertEqual(current["status"], "stale")
        self.assertIsNone(current["checked_tax_profit"])
        self.assertEqual(current["checked"]["calculation"]["after_tax"], "60")

    def test_detail_rounding_and_filter_does_not_redistribute(self):
        database.metadata.create_all(self.engine)
        with self.engine.begin() as db:
            db.execute(
                database.settings.insert().values(
                    key="report-export:" + "a" * 32, value=dict(id="a" * 32, import_state="ready", state="completed")
                )
            )
            db.execute(report.documents.update().values(headers=["订单号"], row_count=3))
            db.execute(
                report.rows.insert(),
                [
                    dict(job_id="a" * 32, position=i, cells=[str(i)], search=str(i), amount=1, quantity=1)
                    for i in range(1, 4)
                ],
            )
            db.execute(
                cost.results.insert(),
                [dict(job_id="a" * 32, position=i, cost=dict(amount="1", net="1")) for i in range(1, 4)],
            )
        self.save_tax(actual="0.01", reference="T", evidence="1分尾差")
        result = table.query(self.engine, {})
        amounts = [Decimal(r["tax_deduction"]) for r in result["rows"]]
        self.assertEqual(sum(amounts), Decimal(".01"))
        filtered = table.query(self.engine, {"keyword": "2"})
        self.assertEqual(filtered["rows"][0]["tax_deduction"], result["rows"][1]["tax_deduction"])
        self.assertEqual(Decimal(result["summary"]["tax_deduction"]["value"]), Decimal(".01"))


if __name__ == "__main__":
    unittest.main()
