"""Clock-safe publication, identical-snapshot reuse and federation-free status reads."""

import tempfile
import time
import unittest
from unittest.mock import patch

from backend.store import Store


class Round3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.sid = self.store.add("fixture", "secret")
        self.store.enable_shop_databases()

    def updated(self, key):
        with self.store.connect() as db:
            return db.execute(
                "SELECT updated FROM business_cache WHERE shop_id=? AND cache_key=?", (self.sid, key)
            ).fetchone()[0]

    def test_clock_rollback_never_moves_updated_backwards(self):
        self.store.write_cache(self.sid, "products:a", {"rows": [{"id": 1}]})
        first = self.updated("products:a")
        with patch("backend.store.time.time", return_value=first - 3600):
            self.store.write_cache(self.sid, "products:b", {"rows": [{"id": 2}]})
        self.assertGreater(self.updated("products:b"), first)

    def test_identical_rows_keep_projection_and_refresh_header(self):
        key = "products:same"
        rows = [{"id": str(n), "goods_id": str(n), "name": "商品" + str(n)} for n in range(5)]
        self.store.write_cache(self.sid, key, {"rows": rows, "complete": True})
        with self.store.connect() as db:
            db.execute("UPDATE table_rows SET search_text='marker' WHERE shop_id=? AND cache_key=?", (self.sid, key))
        time.sleep(0.01)
        self.store.write_cache(self.sid, key, {"rows": rows, "complete": True, "_sync": {"duration_seconds": 3}})
        with self.store.connect() as db:
            marker = db.execute(
                "SELECT count(*) FROM table_rows WHERE shop_id=? AND cache_key=? AND search_text='marker'",
                (self.sid, key),
            ).fetchone()[0]
            snapshot = db.execute(
                "SELECT updated,json_extract(metadata,'$._sync.duration_seconds') AS d FROM table_snapshots WHERE shop_id=? AND cache_key=?",
                (self.sid, key),
            ).fetchone()
        self.assertEqual(marker, 5)  # rows were not rewritten
        self.assertEqual(snapshot["d"], 3)  # header refreshed
        self.assertEqual(snapshot["updated"], self.updated(key))
        changed = rows[:4] + [{"id": "9", "goods_id": "9", "name": "新"}]
        self.store.write_cache(self.sid, key, {"rows": changed, "complete": True})
        with self.store.connect() as db:
            names = [
                r[0]
                for r in db.execute(
                    "SELECT name FROM table_rows WHERE shop_id=? AND cache_key=? ORDER BY ordinal", (self.sid, key)
                )
            ]
        self.assertEqual(names[-1], "新")

    def test_status_ledger_reads_do_not_federate(self):
        from backend import shard_connection
        from backend.system_status import attention_counts

        with patch.object(
            shard_connection.FederatedConnection, "read_all", side_effect=AssertionError("federated read")
        ):
            with self.store.connect() as db:
                attention_counts(self.store, db)


if __name__ == "__main__":
    unittest.main()
