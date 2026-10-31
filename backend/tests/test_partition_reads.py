import json, tempfile, time, unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from backend.store import Store
from backend import collection, home_comparison, operations_insights, query_batch
from backend.business_reader import BEIJING
from backend.invoice_import_store import applications
from backend.partition_reads import rows, read_connection
from backend.shard_connection import FederatedConnection, shop_context


class PartitionReadsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.ids = [self.store.add(str(i), "pw") for i in range(2)]
        self.now = datetime.now(BEIJING)
        self.yesterday = self.now - timedelta(days=1)
        for sid in self.ids:
            self.store.write_cache(
                sid,
                "overview:wanted",
                {
                    "complete": True,
                    "metrics": {"sales": sid * 100},
                    "_sync": {"snapshot_started_at": self.yesterday.timestamp()},
                },
            )
            self.store.write_cache(sid, "products:wanted", {"rows": [{"id": str(sid), "name": "wanted"}]})
            self.store.write_cache(sid, "products:unrelated", {"rows": [{"id": "other", "name": "do not load"}]})
            collection.register(self.store, sid, "products:wanted", "products", {})
            self.store.write_cache(sid, "invoices:old", {"rows": [{"id": "same", "status": "待开票", "amount": 1}]})
            self.store.write_cache(sid, "invoices:new", {"rows": [{"id": "same", "status": "待开票", "amount": 2}]})
            operations_insights.record_run(self.store, sid, "products", time.time() - 2, True)

    def no_federation(self):
        return patch.object(FederatedConnection, "read_all", side_effect=AssertionError("full-table federation used"))

    def test_home_filtered_history_and_comparison_match_legacy(self):
        records = self.store.list()
        keys = {sid: ("overview:wanted", "transactions:missing") for sid in self.ids}
        before = home_comparison.read(self.store, records, [], self.now, keys)
        history = operations_insights.history(self.store)
        self.store.enable_shop_databases()
        with self.no_federation():
            self.assertEqual(home_comparison.read(self.store, records, [], self.now, keys), before)
            self.assertEqual(operations_insights.history(self.store), history)
            one = home_comparison.read(self.store, records[:1], [], self.now, keys)
        self.assertEqual(one["fields"]["sales"]["value"], self.ids[0] * 100)

    def test_batch_only_reads_requested_scopes_and_preserves_indexed_results(self):
        keys = {sid: "products:wanted" for sid in self.ids}
        expected = {indexed: query_batch.read(self.store, keys, indexed) for indexed in (False, True)}
        self.store.enable_shop_databases()
        with self.no_federation():
            for indexed in (False, True):
                self.assertEqual(query_batch.read(self.store, keys, indexed), expected[indexed])
            self.assertEqual(query_batch.read(self.store, {}, False), ({}, {}))

    def test_financial_filter_and_latest_order_are_preserved(self):
        expected = applications(self.store)
        self.store.enable_shop_databases()
        with self.no_federation():
            self.assertEqual(applications(self.store), expected)
        self.assertEqual([r["amount"] for r in expected], [2, 2])

    def test_per_shop_aggregation_never_copies_all_runs(self):
        service = SimpleNamespace(
            store=self.store, session_getter=lambda _: SimpleNamespace(state="logged_in"), progress={}
        )
        expected = operations_insights.health(service)
        self.store.enable_shop_databases()
        with self.no_federation():
            self.assertEqual(operations_insights.health(service), expected)

    def test_read_snapshot_does_not_admit_writer_and_restores_scope(self):
        self.store.enable_shop_databases()
        token = shop_context.set(999)
        try:
            with self.no_federation(), read_connection(self.store, self.ids[0]) as db:
                list(db.execute("SELECT shop_id FROM business_cache WHERE cache_key=?", ("products:wanted",)))
                self.assertFalse(self.store.writer_for(self.ids[0]).status()["active"])
            self.assertEqual(shop_context.get(), 999)
        finally:
            shop_context.reset(token)
