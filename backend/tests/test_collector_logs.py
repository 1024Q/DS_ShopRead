import sqlite3
import tempfile
import unittest
from pathlib import Path
import time
from collections import deque
from unittest.mock import patch
from backend.collector_logs import FileTail, BusinessTail, Reader, LogPanel, redact, level_of


class CollectorLogTests(unittest.TestCase):
    def test_shop_name_is_safe_unique_and_rotates_after_seven_days(self):
        from backend.collector_logs import shop_log_source, DailyLog, BEIJING
        from datetime import datetime

        name = shop_log_source(7, "../同名:店铺\\测试")
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertNotIn(":", name)
        self.assertNotEqual(name, shop_log_source(8, "../同名:店铺\\测试"))
        with tempfile.TemporaryDirectory() as folder:
            old = Path(folder) / (name + "-2026-10-01.log")
            old.write_text("old")
            keep = Path(folder) / (name + "-2026-10-07.log")
            keep.write_text("keep")
            DailyLog(folder, name).write("test", datetime(2026, 10, 13, tzinfo=BEIJING))
            self.assertFalse(old.exists())
            self.assertTrue(keep.exists())

    def test_shop_files_filter_throttle_and_bound_size(self):
        from backend.collector_logs import shop_event, DailyLog

        with tempfile.TemporaryDirectory() as directory:
            shop_event(directory, 1, "info", "orders", "start", "开始读取", "店铺甲")
            self.assertFalse(list(Path(directory).glob("*.log")))
            shop_event(directory, 1, "error", "orders", "failed", "token=secret 读取失败", "店铺甲")
            shop_event(directory, 1, "error", "orders", "failed", "token=secret 读取失败", "店铺甲")
            shop_event(directory, 2, "info", "orders", "done", "读取完成", "店铺乙")
            files = list(Path(directory).glob("*-shop-*.log"))
            self.assertEqual(len(files), 2)
            text = next(p for p in files if p.name.startswith("店铺甲-shop-1-")).read_text(encoding="utf-8")
            self.assertNotIn("secret", text)
            self.assertEqual(text.count("读取失败"), 1)
            writer = DailyLog(directory, "shop-3")
            writer.MAX_BYTES = 100
            for _ in range(10):
                writer.write("x" * 50)
            self.assertEqual(len(list(Path(directory).glob("shop-3-*.log"))), 2)

    def test_partial_utf8_and_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.log"
            tail = FileTail(path)
            self.assertEqual(tail.read(), [])
            raw = "采集成功\n".encode()
            path.write_bytes(raw[:2])
            self.assertEqual(tail.read(), [])
            with path.open("ab") as stream:
                stream.write(raw[2:])
            self.assertEqual(tail.read(), ["采集成功"])
            self.assertEqual(tail.read(), [])
            path.rename(path.with_suffix(".old"))
            path.write_text("rotated\n", encoding="utf-8")
            self.assertEqual(tail.read(), ["rotated"])
            path.write_bytes(b"x\n")
            self.assertEqual(tail.read(), ["x"])

    def test_bounded_initial_read_and_long_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collector.log"
            path.write_bytes(b"old\n" * 1000 + b"new\n")
            tail = FileTail(path)
            rows = tail.read(64)
            self.assertEqual(rows[-1], "new")
            self.assertLess(len(rows), 20)
            with path.open("ab") as stream:
                stream.write(b"x" * 300)
            for _ in range(5):
                tail.read(64)
            self.assertLessEqual(len(tail.pending), 64)

    def test_secrets_and_levels(self):
        result = redact('Bearer abc123 password="hello world" token=abc secret-value', ["secret-value"])
        for secret in ["abc123", "hello world", "secret-value", "token=abc"]:
            self.assertNotIn(secret, result)
        for text, level in [
            ("ERROR crashed", "error"),
            ("正在重连", "warning"),
            ("采集成功", "success"),
            ("DEBUG detail", "debug"),
            ("GET /health", "info"),
        ]:
            self.assertEqual(level_of(text), level)

    def test_business_file_tail_follows_new_events_without_database(self):
        from backend.log_spool import Spool
        import time

        with tempfile.TemporaryDirectory() as directory:
            tail = BusinessTail(Path(directory) / "shops.sqlite3")
            spool = Spool(Path(directory) / "log-spool")
            self.assertEqual(tail.read(), [])

            def append(message):
                spool.append({"created": time.time(), "level": "error", "module": "orders", "message": message})

            append("first")
            self.assertIn("first", tail.read()[0][1])
            self.assertEqual(tail.read(), [])
            append("second")
            self.assertIn("second", tail.read()[0][1])
            self.assertEqual(tail.read(), [])


class ReaderTests(unittest.TestCase):
    def test_single_long_lived_reader_feeds_queue(self):
        import threading

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collector.log"
            path.write_text("first\n", encoding="utf-8")
            before = threading.active_count()
            reader = Reader({"采集服务": FileTail(path)}, None, interval=0.02).start()
            try:
                rows = []
                for _ in range(100):
                    rows += reader.drain()
                    if rows:
                        break
                    time.sleep(0.02)
                self.assertEqual(rows, [("info", "采集服务", "first")])
                with path.open("a", encoding="utf-8") as stream:
                    stream.write("ERROR second\n")
                for _ in range(100):
                    rows += reader.drain()
                    if len(rows) > 1:
                        break
                    time.sleep(0.02)
                self.assertEqual(rows[-1], ("error", "采集服务", "ERROR second"))
                self.assertLessEqual(threading.active_count(), before + 1)
            finally:
                reader.stop()
                reader.thread.join(2)
            self.assertFalse(reader.thread.is_alive())


class PanelTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk

        try:
            self.window = tk.Tk()
        except tk.TclError:
            self.skipTest("no Tk display")
        self.window.withdraw()
        self.temp = tempfile.TemporaryDirectory()
        self.panel = LogPanel(self.window, self.temp.name, lambda: [])

    def tearDown(self):
        self.panel.reader.stop()
        self.window.destroy()
        self.temp.cleanup()

    def lines(self):
        return self.panel.text.get("1.0", "end-1c").splitlines()

    def test_append_only_and_trim_from_top(self):
        self.panel.LIMIT = 5
        self.panel.rows = deque(maxlen=5)
        self.panel.add([("info", "s", f"line {i}") for i in range(3)])
        with patch.object(self.panel.text, "delete", wraps=self.panel.text.delete) as delete:
            self.panel.add([("info", "s", f"line {i}") for i in range(3, 8)])
            self.assertEqual(delete.call_args.args, ("1.0", "4.0"))  # Only trims the oldest lines; never a full redraw.
        self.assertEqual(self.lines(), [f"[s] line {i}" for i in range(3, 8)])

    def test_filter_pause_and_rerender(self):
        self.panel.filter.set("错误")
        self.panel.add([("info", "s", "quiet"), ("error", "s", "loud")])
        self.assertEqual(self.lines(), ["[s] loud"])
        self.panel.pause.set(True)
        self.panel.add([("error", "s", "later")])
        self.assertEqual(self.lines(), ["[s] loud"])
        self.panel.pause.set(False)
        self.panel.filter.set("全部")
        self.panel.render()
        self.assertEqual(self.lines(), ["[s] quiet", "[s] loud", "[s] later"])


if __name__ == "__main__":
    unittest.main()


class DailyLogsTests(unittest.TestCase):
    def test_status_classification(self):
        self.assertEqual(level_of("中心已连接 · 服务异常重启 0 次"), "success")
        self.assertEqual(level_of("INFO 中心已连接 · 服务异常重启 0 次"), "info")
        self.assertEqual(level_of("ERROR 服务异常重启 1 次"), "error")

    def test_daily_rotation_retention_and_redaction(self):
        from backend.collector_logs import DailyLog, BEIJING
        from datetime import datetime, timedelta

        with tempfile.TemporaryDirectory() as folder:
            writer = DailyLog(folder, "collector", ["sensitive"])
            start = datetime(2026, 10, 1, 23, 59, tzinfo=BEIJING)
            for i in range(9):
                writer.write("INFO sensitive", start + timedelta(days=i))
            paths = sorted(Path(folder).glob("*.log"))
            self.assertEqual(len(paths), 7)
            self.assertEqual(paths[0].name, "collector-2026-10-03.log")
            text = paths[-1].read_text(encoding="utf-8")
            self.assertTrue(text.startswith("2026-10-09 23:59:00"))
            self.assertNotIn("sensitive", text)
