import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from backend.database_writer import WriterQueue
from backend.store import Store
from backend.collection_checkpoint import Checkpoint
from backend.collection_errors import classify, describe


class WriterTests(unittest.TestCase):
    def test_fifo_and_expired_waiter_do_not_starve_next_writer(self):
        queue = WriterQueue()
        queue.acquire()
        results = []

        def work(index, timeout):
            if queue.acquire(timeout):
                results.append(index)
                queue.release()

        threads = []
        for index in range(4):
            thread = threading.Thread(target=work, args=(index, 0.5 if index == 0 else 4))
            thread.start()
            threads.append(thread)
            deadline = time.monotonic() + 1
            while len(queue.waiters) < index + 1 and time.monotonic() < deadline:
                time.sleep(0.001)
        threads[0].join(2)
        self.assertFalse(threads[0].is_alive())
        queue.release()
        for thread in threads:
            thread.join(3)
        self.assertEqual(results, [1, 2, 3])
        self.assertFalse(queue.waiters)

    def test_checkpoint_retries_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            checkpoint = Checkpoint(store, 1, "orders:test", {})
            checkpoint.data["parts"]["one"] = {"rows": {"a": {"id": "a"}}, "ids": ["a"], "loaded": 1, "total": 1}
            original = store.write_connection
            calls = []

            def write():
                calls.append(1)
                if len(calls) == 1:
                    raise sqlite3.OperationalError("database writer is busy")
                return original()

            with patch.object(store, "write_connection", side_effect=write):
                checkpoint.save()
            restored = Checkpoint(store, 1, "orders:test", {})
            self.assertEqual(restored.data["parts"]["one"]["rows"], {"a": {"id": "a"}})
            self.assertEqual(len(calls), 2)
            self.assertFalse(checkpoint.dirty)

    def test_checkpoint_does_not_retry_non_lock_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            checkpoint = Checkpoint(Store(folder), 1, "orders:test", {})
            with patch.object(checkpoint, "_save", side_effect=sqlite3.OperationalError("disk I/O error")) as save:
                with self.assertRaises(sqlite3.OperationalError):
                    checkpoint.save()
                self.assertEqual(save.call_count, 1)

    def test_busy_error_is_not_browser_failure(self):
        error = sqlite3.OperationalError("database writer is busy")
        self.assertEqual(classify(error), ("database_busy", False))
        self.assertIn("数据库写入繁忙", describe(error))


class UnifiedWriterTests(unittest.TestCase):
    def test_shared_path_legacy_cursor_and_wal_read(self):
        from backend.database_writer import connection, queue_for

        with tempfile.TemporaryDirectory() as folder:
            a = Store(folder)
            b = Store(folder)
            self.assertIs(a._write_lock, b._write_lock)
            started = threading.Event()
            done = threading.Event()

            def legacy():
                started.set()
                with b.connect() as db:
                    db.cursor().execute("INSERT INTO settings VALUES('writer-test','yes')")
                done.set()

            with a.write_connection() as db:
                db.execute("INSERT INTO settings VALUES('held','yes')")
                t = threading.Thread(target=legacy)
                t.start()
                started.wait(1)
                deadline = time.monotonic() + 1
                while not a._write_lock.status()["pending"] and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(a._write_lock.status()["pending"], 1)
                with b.connect() as reader:
                    self.assertIsNone(reader.execute("SELECT value FROM settings WHERE key='held'").fetchone())
                self.assertFalse(done.is_set())
            t.join(2)
            self.assertTrue(done.is_set())
            with b.connect() as db:
                self.assertEqual(db.execute("SELECT value FROM settings WHERE key='writer-test'").fetchone()[0], "yes")
            self.assertFalse(a._write_lock.status()["active"])

    def test_pressure_and_rollback_release(self):
        from backend.database_writer import connection, queue_for

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            gate = store._write_lock
            with self.assertRaises(ValueError):
                with store.write_connection() as db:
                    db.execute("INSERT INTO settings VALUES('rolled-back','yes')")
                    gate.started -= 6
                    self.assertTrue(gate.status()["pressure"])
                    raise ValueError("rollback")
            self.assertFalse(gate.status()["pressure"])
            with store.connect() as db:
                self.assertIsNone(db.execute("SELECT value FROM settings WHERE key='rolled-back'").fetchone())

    def test_other_process_gate_times_out_then_recovers(self):
        import subprocess, sys
        from backend.database_writer import connection

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            code = "from backend.database_writer import connection; import sys,sqlite3\ntry:\n with connection(sys.argv[1],timeout=.2,immediate=True):pass\nexcept sqlite3.OperationalError:print('BUSY')\n"
            with store.write_connection():
                result = subprocess.run(
                    [sys.executable, "-c", code, str(store.path)], capture_output=True, text=True, timeout=5
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "BUSY")
            with connection(store.path, timeout=0.2, immediate=True) as db:
                db.execute("INSERT INTO settings VALUES('recovered','yes')")


class PressureAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_backlog_pauses_new_reads_and_recovers_without_config_change(self):
        import asyncio
        from types import SimpleNamespace
        from backend.business import BusinessService
        from backend import collection

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            service = BusinessService(store, None)
            config = dict(collection.DEFAULTS)
            service.configure_queue(config)
            item = SimpleNamespace(lock=asyncio.Lock(), shop={"name": "测试店铺"})
            started = asyncio.Event()

            async def run():
                async with service.slot((1, "overview"), item, "overview"):
                    started.set()

            with patch.object(store._write_lock, "status", return_value={"pressure": True}):
                task = asyncio.create_task(run())
                await asyncio.sleep(0.05)
                self.assertFalse(started.is_set())
                self.assertEqual(service.progress[(1, "overview")]["wait_code"], "database")
            service.wake.set()
            await asyncio.wait_for(task, 2)
            self.assertTrue(started.is_set())
            self.assertEqual(service.queue_config, config)


class WritePanelTests(unittest.TestCase):
    def test_status_and_file_only_logging(self):
        from backend.collector_overview import normalize, write_hint

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            values, config = normalize({"summary": {}, "database_write": store._write_lock.status()})
            self.assertIn("待写 0 批", write_hint(config["database_write"]))
            store.log_event("warning", "database", "slow_write", "慢写入测试")
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM event_logs").fetchone()[0], 0)
            from pathlib import Path

            self.assertTrue(list(Path(folder).rglob("*.jsonl")))
