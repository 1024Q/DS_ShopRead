import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.store import Store
from backend.log_spool import Spool, get, reason
from backend.collector_overview import log_hint


class LogSpoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.spool = get(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, message="test", stamp=None):
        self.spool.append(
            dict(
                created=stamp or time.time(),
                level="error",
                module="orders",
                event="failure",
                message=message,
                shop_id=None,
            )
        )

    def test_busy_database_never_blocks_files_or_writes_log_rows(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            self.store.log_event("error", "orders", "failure", "locked password=secret")
            self.spool.flush(self.store)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(db.execute("SELECT count(*) FROM event_logs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM log_spool_cursors").fetchone()[0], 0)
        self.assertNotIn("secret", self.spool.files()[0].read_text("utf-8"))
        reopened = Store(self.tmp.name)
        self.assertEqual(len(reopened.read_logs()), 1)
        self.assertEqual(Spool(self.spool.folder).flush(reopened)["storage"], "files")

    def test_filter_cursor_partial_lines_and_noise(self):
        for message in ("one", "two", "three"):
            self.event(message)
        self.store.log_event("info", "orders", "sync_start", "skip")
        first = self.store.read_logs(level="error", module="orders", limit=2)
        second = self.store.read_logs(before=first[-1]["last_seen"], before_id=first[-1]["id"])
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 1)
        self.assertEqual(len({r["id"] for r in first + second}), 3)
        with self.spool.files()[0].open("ab") as stream:
            stream.write(b"{unfinished")
        self.assertEqual(len(self.store.read_logs()), 3)

    def test_seven_day_retention_does_not_access_database(self):
        now = time.time()
        self.event("expired", now - 8 * 86400)
        self.event("keep", now - 6 * 86400)
        with patch.object(self.store, "connect", side_effect=AssertionError("no database")):
            state = self.spool.flush(self.store, now=now)
        self.assertEqual(len(self.spool.files()), 1)
        self.assertEqual(state["pending"], 0)
        self.assertIn("仅保存文件", log_hint(state))
        self.assertEqual([r["message"] for r in self.store.read_logs()], ["keep"])

    def test_file_failure_is_visible_and_reasons_are_specific(self):
        with patch.object(Path, "open", side_effect=PermissionError(13, "denied")):
            self.assertFalse(self.spool.append(dict(created=time.time(), message="safe")))
        self.assertIn("权限", self.spool.status()["error"])
        self.assertIn("磁盘空间不足", reason(sqlite3.OperationalError("database or disk is full")))
