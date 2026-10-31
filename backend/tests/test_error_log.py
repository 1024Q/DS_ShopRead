import io, tempfile, unittest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch
from backend.collector_logs import DailyLog, ProcessLog, BEIJING, shop_event


class ErrorLogTests(unittest.TestCase):
    def test_errors_merged_redacted_and_normal_logs_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            DailyLog(folder, "collector", ("private-value",)).write("ERROR crash token=abc private-value")
            DailyLog(folder, "agent").write("INFO normal")
            shop_event(folder, 9, "error", "orders", "failure", "query failed", "shop-name")
            files = list(Path(folder).glob("errors-*.log"))
            self.assertEqual(len(files), 1)
            text = files[0].read_text(encoding="utf-8")
            self.assertIn("[collector]", text)
            self.assertIn("shop-name-shop-9", text)
            self.assertNotIn("normal", text)
            self.assertNotIn("private-value", text)
            self.assertNotIn("token=abc", text)
            self.assertEqual(len(list(Path(folder).glob("collector-*.log"))), 1)

    def test_traceback_keeps_stack_and_stops_at_next_record(self):
        with tempfile.TemporaryDirectory() as folder:
            log = ProcessLog(
                io.BytesIO(
                    b'ERROR crashed\nTraceback (most recent call last):\n  File "example.py", line 1\n    fail()\nValueError: bad\nINFO recovered\n'
                ),
                folder,
                "collector",
            )
            log.close()
            text = next(Path(folder).glob("errors-*.log")).read_text(encoding="utf-8")
            self.assertIn("example.py", text)
            self.assertIn("fail()", text)
            self.assertIn("ValueError", text)
            self.assertNotIn("recovered", text)

    def test_rotation_retention_and_copy_failure_do_not_break_original(self):
        with tempfile.TemporaryDirectory() as folder:
            old = Path(folder) / "errors-2000-01-01.log"
            old.write_text("old")
            with patch.object(DailyLog, "MAX_BYTES", 150):
                for i in range(8):
                    DailyLog(folder, "collector").write("ERROR " + str(i) + "x" * 80)
            self.assertFalse(old.exists())
            self.assertEqual(len(list(Path(folder).glob("errors-*.log"))), 2)
            with patch("backend.error_log.Path.open", side_effect=PermissionError()):
                from backend.error_log import append

                append(folder, "ERROR inaccessible")


if __name__ == "__main__":
    unittest.main()
