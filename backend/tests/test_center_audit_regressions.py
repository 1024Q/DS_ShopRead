import asyncio, tempfile, time, unittest, zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from sqlalchemy import create_engine, select, event, func
from sqlalchemy.exc import OperationalError
from fastapi import FastAPI
from backend.center import (
    report_view as view,
    profit_tax as tax,
    profit,
    reconciliation as cost,
    cost_supplements as supplements,
    report_exports as exports,
)
from backend.center.database import Database, metadata as database_meta, settings
from backend.center.cost_tables import meta as cost_meta


class ReportSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.engine = create_engine("sqlite:///" + str(self.root / "test.db"))
        for meta in (view.meta, tax.meta, profit.meta, cost.meta, supplements.meta):
            meta.create_all(self.engine)
        self.job = dict(
            id="a" * 32,
            shop_id=1,
            shop_name="店铺甲",
            created=1,
            start="2026-09-01 00:00:00",
            end="2026-10-01 00:00:00",
        )

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def package(self, body, second=None):
        path = self.root / "report.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("report.csv", "商品,订单号,商家实收金额(元)\n" + body)
            if second:
                z.writestr("part2.csv", second)
        return path

    def test_failed_reimport_keeps_old_rows_even_after_a_staging_batch(self):
        view.import_report(self.engine, self.job, self.package("原商品,123,10\n"))
        path = self.package("".join(f"新商品,{n},20\n" for n in range(1001)), "错误表头\nwrong")
        with self.assertRaises(ValueError):
            view.import_report(self.engine, self.job, path)
        result = view.query(self.engine, [self.job["id"]])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["rows"][0]["cells"][0], "原商品")
        with self.engine.connect() as db:
            self.assertEqual(db.execute(select(func.count()).select_from(view.rows)).scalar(), 1)
            self.assertEqual(db.execute(select(func.count()).select_from(view.documents)).scalar(), 1)
        view.import_report(self.engine, self.job, self.package("替换商品,456,30\n"))
        self.assertEqual(view.query(self.engine, [self.job["id"]])["rows"][0]["cells"][0], "替换商品")
        summary = view.query(self.engine, [self.job["id"]], metadata_only=True)
        self.assertEqual(summary["rows"], [])
        self.assertEqual(summary["total"], 1)

    def test_current_missing_excludes_superseded_report_but_history_keeps_it(self):
        old = self.job
        latest = {**old, "id": "b" * 32, "created": 2}
        with self.engine.begin() as db:
            for doc in (old, latest):
                db.execute(view.documents.insert().values(**doc, ready=1, headers=[], row_count=1))
            db.execute(cost.bindings.insert().values(job_id=old["id"], summary=dict(shop_id=1, shop_name="店铺甲")))
            db.execute(
                cost.results.insert().values(
                    job_id=old["id"],
                    position=1,
                    cost=dict(sku="SKU", product="旧商品", spec="规格", state="未匹配成本", total=None),
                )
            )
        self.assertEqual(supplements.missing(self.engine), [])
        history = supplements.missing(self.engine, "history")
        self.assertEqual(history[0]["reports"][0]["job_id"], old["id"])
        self.assertEqual(history[0]["reports"][0]["month"], "2026-09")
        database_meta.create_all(self.engine)
        with self.engine.begin() as db:
            for doc in (old, latest):
                db.execute(
                    settings.insert().values(
                        key="report-export:" + doc["id"],
                        value=dict(id=doc["id"], import_state="ready", state="completed"),
                    )
                )
            db.execute(
                settings.insert().values(key="report-export-delete:" + latest["id"], value=dict(id=latest["id"]))
            )
        self.assertEqual(supplements.missing(self.engine)[0]["reports"][0]["job_id"], old["id"])

    def test_staging_cleanup_failure_does_not_report_committed_import_as_failed(self):
        def fail_cleanup(connection, cursor, statement, parameters, context, many):
            if statement.startswith("DELETE FROM report_document_rows") and parameters[0] != self.job["id"]:
                raise OperationalError(statement, parameters, RuntimeError("cleanup busy"))

        event.listen(self.engine, "before_cursor_execute", fail_cleanup)
        try:
            with self.assertLogs("ecmp.center", level="ERROR"):
                count = view.import_report(self.engine, self.job, self.package("已提交,123,10\n"))
            self.assertEqual(count, 1)
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_cleanup)
        self.assertEqual(view.query(self.engine, [self.job["id"]])["rows"][0]["cells"][0], "已提交")
        with self.engine.begin() as db:
            db.execute(view.documents.update().where(view.documents.c.ready == 0).values(created=0))
        view.cleanup_staging(self.engine)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(select(func.count()).select_from(view.documents)).scalar(), 1)

    def test_reimport_invalidates_amounts_but_preserves_pinned_cost_and_tax_versions(self):
        view.import_report(self.engine, self.job, self.package("原商品,123,10\n"))
        with self.engine.begin() as db:
            db.execute(
                cost.bindings.insert().values(
                    job_id=self.job["id"],
                    book_id="book-version",
                    income_mode="row",
                    summary=dict(revenue="10", rows=1),
                    updated=1,
                )
            )
            db.execute(cost.results.insert().values(job_id=self.job["id"], position=1, cost=dict(total="3")))
            db.execute(
                tax.reports.insert().values(
                    job_id=self.job["id"], version_id="tax-version", signature="old", summary=dict(deduction="1")
                )
            )
            db.execute(tax.allocations.insert().values(job_id=self.job["id"], position=1, deduction=1))
        view.import_report(self.engine, self.job, self.package("新商品,123,20\n"))
        with self.engine.connect() as db:
            binding = db.execute(select(cost.bindings)).mappings().one()
            policy = db.execute(select(tax.reports)).mappings().one()
            self.assertEqual(binding["book_id"], "book-version")
            self.assertEqual(binding["income_mode"], "row")
            self.assertIsNone(binding["summary"])
            self.assertEqual(policy["version_id"], "tax-version")
            self.assertEqual(policy["signature"], "")
            for table in (cost.results, tax.allocations):
                self.assertEqual(db.execute(select(func.count()).select_from(table)).scalar(), 0)
        from backend.center.promotion_ledger import meta as promotion_meta
        from backend.center import table_tools

        promotion_meta.create_all(self.engine)
        with self.engine.connect() as db:
            self.assertIsNone(profit.present(profit.sources(db, "2026-09")[1])["calculation"]["estimated"])
        detail = table_tools.query(self.engine, {"month": "2026-09"})
        self.assertEqual(detail["rows"][0]["raw_0"], "新商品")
        self.assertIsNone(detail["rows"][0]["cost_total"])
        summary = cost.enrich_reports(self.engine, view.query(self.engine, [self.job["id"]], metadata_only=True))
        self.assertEqual(summary["accounting_pending"], ["店铺甲"])
        self.assertEqual(summary["accounting"], [])

    def test_current_tax_reads_are_batched_and_single_shop_scope_is_respected(self):
        with self.engine.begin() as db:
            for sid in range(1, 51):
                jid = f"{sid:032x}"
                db.execute(
                    view.documents.insert().values(
                        **{**self.job, "id": jid, "shop_id": sid}, ready=1, headers=[], row_count=0
                    )
                )
                db.execute(
                    cost.bindings.insert().values(
                        job_id=jid, updated=1, summary=dict(revenue="10", revenue_complete=True)
                    )
                )
        tax.sync(self.engine)
        statements = []

        def record(connection, cursor, statement, parameters, context, many):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", record)
        try:
            tax.sync(self.engine)
        finally:
            event.remove(self.engine, "before_cursor_execute", record)
        self.assertLessEqual(len(statements), 5)
        self.assertFalse(any(s.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for s in statements))
        with self.engine.begin() as db:
            db.execute(tax.reports.update().values(signature="stale"))
        tax.sync(self.engine, month="2026-09", shop_id=1)
        with self.engine.connect() as db:
            self.assertNotEqual(
                db.execute(select(tax.reports.c.signature).where(tax.reports.c.job_id == f"{1:032x}")).scalar(), "stale"
            )
            self.assertEqual(
                db.execute(select(tax.reports.c.signature).where(tax.reports.c.job_id == f"{2:032x}")).scalar(), "stale"
            )


class WorkerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_database_error_recovers_without_center_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = Database("sqlite:///" + str(root / "center.db"))
            cost_meta.create_all(database.engine)
            failed = []

            def once(connection, cursor, statement, parameters, context, many):
                if not failed and "cost_catalog" in statement:
                    failed.append(True)
                    raise OperationalError(statement, parameters, RuntimeError("temporary failure"))

            try:
                with patch.object(exports, "ROOT", root):
                    app = FastAPI()
                    exports.install(app, database, SimpleNamespace(peers={}), lambda: None)
                    event.listen(database.engine, "before_cursor_execute", once)
                    async with app.router.lifespan_context(app):
                        await asyncio.sleep(0.1)
                        self.assertTrue(failed)
                        self.assertFalse(app.state.report_export_task.done())
                        await asyncio.sleep(2.1)
                        self.assertFalse(app.state.report_export_task.done())
                        route = next(
                            r.endpoint for r in app.routes if r.path == "/api/report-exports" and "GET" in r.methods
                        )
                        health = route()["worker"]
                        self.assertGreater(health["last_success"], 0)
                        self.assertEqual(health["consecutive_failures"], 0)
            finally:
                event.remove(database.engine, "before_cursor_execute", once)
                database.engine.dispose()
