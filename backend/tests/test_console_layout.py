import tempfile
import unittest
import tkinter as tk
from tkinter import ttk
from backend.collector_launcher import console_panels


class LayoutTests(unittest.TestCase):
    def test_log_tabs_survive_resize_without_task_table(self):
        root = tk.Tk()
        root.geometry("1000x560+0+0")
        ttk.Style().theme_use("clam")
        ttk.Style().configure(".", font=("Microsoft YaHei UI", 10))
        ttk.Style().configure("TButton", padding=(10, 6))
        ttk.Style().configure("Treeview", rowheight=27)
        try:
            with tempfile.TemporaryDirectory() as directory:
                # Reserve space for launcher header, collapsed connection, controls and status bar.
                ttk.Frame(root, height=180).pack(fill="x")
                panes, overview, logs = console_panels(root, directory, lambda: [])
                panes.pack(fill="both", expand=True)
                overview.detail.set("定时采集已开启\n重试冷却与到期待执行")
                overview.logs.set("数据库写入状态 " * 60)
                root.update()
                for height in (560, 900, 650, 560):
                    root.geometry(f"1000x{height}+0+0")
                    root.update()
                    self.assertFalse(overview.frame.winfo_ismapped())
                    for tab, panel in ((0, logs.operations), (1, logs)):
                        logs.notebook.select(tab)
                        root.update()
                        self.assertGreaterEqual(panel.text.winfo_height(), 60)
                        self.assertLessEqual(
                            panel.frame.winfo_rooty() + panel.frame.winfo_height(),
                            root.winfo_rooty() + root.winfo_height(),
                        )
                logs.reader.stop()
                logs.reader.thread.join(2)
                logs.operations.reader.stop()
                logs.operations.reader.thread.join(2)
        finally:
            root.destroy()
