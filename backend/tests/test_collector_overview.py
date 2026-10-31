import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from backend.collector_overview import normalize, read_snapshot, OverviewPanel


class OverviewTests(unittest.TestCase):
    def test_unknown_counts_are_not_zero(self):
        values, config = normalize({"summary": {"running": 0, "queued": 3, "errors": True, "cached": -1}})
        self.assertEqual(values["running"], 0)
        self.assertEqual(values["queued"], 3)
        self.assertIsNone(values["errors"])
        self.assertIsNone(values["cached"])
        self.assertIsNone(values["waiting_login"])
        with self.assertRaises(ValueError):
            normalize({})

    def test_read_uses_only_local_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "api.key").write_text("local-test-key")
            response = httpx.Response(
                200,
                json={"summary": {"running": 4}, "collection": {"enabled": True}},
                request=httpx.Request("GET", "http://localhost/"),
            )
            with patch("backend.collector_overview.httpx.get", return_value=response) as get:
                values, config, update, stamp = read_snapshot(directory, 18001)
                self.assertEqual(get.call_args.args[0], "http://127.0.0.1:18001/api/system/summary")
                self.assertFalse(get.call_args.kwargs["trust_env"])
                self.assertEqual(values["running"], 4)

    def test_gui_keeps_last_good_values_and_discards_old_process_result(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            panel = OverviewPanel(root, Path("."))
            panel.was_running = True
            panel.next_poll = float("inf")
            values, config = normalize(
                {"summary": {"running": 2, "errors": 1}, "collection": {"enabled": True, "max_concurrent": 4}}
            )
            panel.queue.put((0, (values, config, {}, time.time()), ""))
            panel.tick(True, 8001, 3)
            self.assertEqual(panel.values["running"].cget("text"), "2")
            panel.queue.put((0, None, "unavailable"))
            panel.tick(True, 8001, 3)
            self.assertEqual(panel.values["running"].cget("text"), "2")
            self.assertIn("上次", panel.hint.get())
            panel.tick(False, 8001, 3)
            self.assertEqual(panel.values["running"].cget("text"), "—")
            panel.queue.put((0, (values, config, {}, time.time()), ""))
            panel.tick(False, 8001, 3)
            self.assertEqual(panel.values["running"].cget("text"), "—")
        finally:
            root.destroy()
