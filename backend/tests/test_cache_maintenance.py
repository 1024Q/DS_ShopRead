import sqlite3, tempfile, time, unittest
from unittest.mock import patch
from backend.store import Store
from backend.center.replication_worker import Delivery


class CacheMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.sid = self.store.add("fixture", "secret")
        self.store.enable_shop_databases()

    def test_external_lock_is_immediate_and_error_is_specific(self):
        blocker = sqlite3.connect(self.store._shards.path(self.sid))
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with self.assertLogs("backend.store", level="WARNING") as logs:
            self.store._prune_cache(self.sid)
        self.assertLess(time.monotonic() - started, 1)
        self.assertIn("SQLITE_BUSY", logs.output[0])
        self.assertIn("database is locked", logs.output[0])
        # Failure is throttled as well as success.
        with patch("backend.database_writer.connection", side_effect=AssertionError("must cool down")):
            self.store._prune_cache(self.sid)

    def test_cleanup_error_keeps_committed_data_available_for_delivery(self):
        with (
            patch("backend.table_index.prune_batch", side_effect=sqlite3.OperationalError("fixture maintenance failure")),
            patch("backend.center.delivery_notify.notify") as notify,
        ):
            with self.assertLogs("backend.store", level="WARNING") as logs:
                self.store.write_cache(self.sid, "products:test", {"rows": [{"id": "kept"}]})
            notify.assert_called_once()
        self.assertIn("fixture maintenance failure", logs.output[0])
        self.assertEqual(self.store.read_cache(self.sid, "products:test")["payload"]["rows"][0]["id"], "kept")
        delivery = Delivery(self.store._shards.path(self.sid).parent, "business", ["url", "node"])
        self.assertTrue(any(r["scope"] == "products:test" for r in delivery.collect()))

    def test_prune_removes_only_old_ordinary_snapshots_on_one_shop(self):
        from backend.store import CACHE_LIMIT

        other = self.store.add("second", "secret")
        self.store.enable_shop_databases()
        for sid in (self.sid, other):
            with self.store.write_connection() as db:
                for n in range(CACHE_LIMIT + 3):
                    db.execute("INSERT INTO business_cache VALUES(?,?,?,?)", (sid, "products:" + str(n), "{}", n))
                db.execute("INSERT INTO business_cache VALUES(?,?,?,?)", (sid, "tax:kept", "{}", 0))
        self.store._prune_cache(self.sid)
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM business_cache WHERE shop_id=?", (self.sid,)).fetchone()[0],
                CACHE_LIMIT + 1,
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM business_cache WHERE shop_id=?", (other,)).fetchone()[0],
                CACHE_LIMIT + 4,
            )

    def test_orphaned_projection_rows_are_removed_in_committed_slices(self):
        from backend import store as store_module

        with self.store.write_connection() as db:
            for n in range(12):
                key = "products:old" + str(n)
                db.execute("INSERT INTO table_snapshots VALUES(?,?,?,?,?)", (self.sid, key, n, "{}", 300))
                db.executemany(
                    "INSERT INTO table_rows(shop_id,cache_key,ordinal,payload) VALUES(?,?,?,?)",
                    [(self.sid, key, i, "{}") for i in range(300)],
                )
        # Control elapsed time, not disk speed: opening SQLite alone can exceed
        # 1 ms on Windows. Each completed slice exhausts the outer time budget
        # without exhausting its separate transaction deadline.
        from types import SimpleNamespace
        from backend.table_index import prune_batch

        clock = [0.0]

        def one_slice(*args):
            more = prune_batch(*args)
            clock[0] += 0.01
            return more

        with (
            patch.object(store_module, "CACHE_PRUNE_SECONDS", 0.001),
            patch.object(store_module, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time)),
            patch("backend.table_index.prune_batch", side_effect=one_slice),
        ):
            self.store._prune_cache(self.sid)
            with self.store.connect() as db:
                remaining = db.execute("SELECT count(*) FROM table_rows WHERE shop_id=?", (self.sid,)).fetchone()[0]
            self.assertGreater(remaining, 0)
            self.assertLess(remaining, 3600)  # first slice is committed despite yielding
            for _ in range(40):
                self.store._cache_pruned.clear()
                self.store._prune_cache(self.sid)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM table_rows WHERE shop_id=?", (self.sid,)).fetchone()[0], 0)
            self.assertEqual(
                db.execute("SELECT count(*) FROM table_snapshots WHERE shop_id=?", (self.sid,)).fetchone()[0], 0
            )

    def test_busy_writer_skips_maintenance(self):
        with (
            patch.object(
                self.store.writer_for(self.sid),
                "status",
                return_value={"active": True, "pending": 0, "pressure": False},
            ),
            patch("backend.database_writer.connection", side_effect=AssertionError("no cleanup admission")),
        ):
            self.store._prune_cache(self.sid)

    def test_slow_cleanup_yields_and_rolls_back_deletions(self):
        from backend.store import CACHE_LIMIT

        with self.store.write_connection() as db:
            for n in range(CACHE_LIMIT + 3):
                db.execute("INSERT INTO business_cache VALUES(?,?,?,?)", (self.sid, "products:" + str(n), "{}", n))

        def slow(db, sid, *args):
            db.execute(
                "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<10000000) SELECT sum(x) FROM n"
            ).fetchone()

        started = time.monotonic()
        with (
            patch("backend.table_index.prune_batch", side_effect=slow),
            self.assertLogs("backend.store", level="INFO") as logs,
        ):
            self.store._prune_cache(self.sid)
        self.assertLess(time.monotonic() - started, 1)
        self.assertIn("SQLITE_INTERRUPT", logs.output[0])
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM business_cache WHERE shop_id=?", (self.sid,)).fetchone()[0],
                CACHE_LIMIT + 3,
            )


if __name__ == "__main__":
    unittest.main()
