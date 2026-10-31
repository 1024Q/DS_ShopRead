import tempfile
import time
import unittest
from pathlib import Path

from backend.center.database import Database, snapshots
from sqlalchemy import select


class ClockRollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database("sqlite:///" + str(Path(self.tmp.name) / "c.sqlite3"))
        self.node = self.db.register("n")["id"]

    def tearDown(self):
        self.db.engine.dispose()
        self.tmp.cleanup()

    def stored(self):
        with self.db.engine.connect() as c:
            return c.execute(
                select(snapshots.c.updated, snapshots.c.payload).where(snapshots.c.scope == "orders")
            ).one()

    def put(self, updated, value):
        self.db.ingest(self.node, [{"shop": "1", "scope": "orders", "updated": updated, "payload": {"v": value}}])

    def test_older_capture_never_replaces_newer(self):
        now = time.time()
        self.put(now, "new")
        self.put(now - 60, "late retry")
        self.assertEqual(self.stored().payload["v"], "new")

    def test_capture_after_clock_rollback_replaces_future_timestamp(self):
        now = time.time()
        self.put(now + 3600, "written while clock was an hour fast")
        self.put(now, "after correction")
        self.assertEqual(self.stored().payload["v"], "after correction")


if __name__ == "__main__":
    unittest.main()
