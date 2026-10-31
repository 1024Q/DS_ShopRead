from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from backend.center.replication_worker import Delivery


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        with closing(sqlite3.connect(self.folder / "shops.sqlite3")) as db, db:
            db.execute(
                "CREATE TABLE business_cache(shop_id INTEGER,cache_key TEXT,payload TEXT,updated REAL,PRIMARY KEY(shop_id,cache_key))"
            )
        self.worker = Delivery(self.folder, "business", ["center", "node"])

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, key, stamp, payload="{}"):
        with closing(sqlite3.connect(self.folder / "shops.sqlite3")) as db, db:
            db.execute("INSERT OR REPLACE INTO business_cache VALUES(1,?,?,?)", (key, payload, stamp))

    async def test_old_late_write_and_restart_are_not_lost(self):
        self.write("new", 10000)
        self.worker.commit(self.worker.collect())
        self.write("late", 1)
        restarted = Delivery(self.folder, "business", ["center", "node"])
        self.assertEqual([r["scope"] for r in restarted.collect()], ["late"])
        restarted.commit(restarted.collect())
        self.assertEqual(Delivery(self.folder, "business", ["center", "node"]).collect(), [])

    async def test_rejected_record_does_not_block_other_records(self):
        for key in ("bad", "good"):
            self.write(key, 1)

        def handler(request):
            rows = json.loads(request.content)["records"]
            return httpx.Response(422 if any(r["scope"] == "bad" for r in rows) else 200)

        async with httpx.AsyncClient(base_url="http://center", transport=httpx.MockTransport(handler)) as client:
            await self.worker.send(client, self.worker.collect())
        self.assertIn(self.worker.key(1, "good"), self.worker.acks)
        self.assertNotIn(self.worker.key(1, "bad"), self.worker.acks)
        self.assertEqual(self.worker.collect(), [])
        self.assertEqual(self.worker.pending, 1)
        self.assertEqual([r["scope"] for r in self.worker.collect(now=10**12)], ["bad"])

    async def test_center_restore_and_missed_ack_reconcile(self):
        self.write("saved", 1)
        self.write("lost", 2)
        self.worker.commit(self.worker.collect())
        async with httpx.AsyncClient(
            base_url="http://center",
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"records": [{"shop": "1", "scope": "saved", "updated": 1}]})
            ),
        ) as client:
            await self.worker.reconcile(client)
        self.assertEqual([r["scope"] for r in self.worker.collect()], ["lost"])

    async def test_payload_budget_sends_large_record_alone(self):
        self.write("a", 1, json.dumps({"text": "a" * 1500000}))
        self.write("b", 2, json.dumps({"text": "b" * 1500000}))
        self.assertEqual(len(self.worker.collect()), 1)


class PanelLayoutTests(unittest.TestCase):
    def test_task_selection_keeps_log_visible_and_can_collapse(self):
        import tkinter as tk
        from backend.collector_launcher import console_panels

        with tempfile.TemporaryDirectory() as folder:
            root = tk.Tk()
            root.geometry("900x470+5000+5000")
            try:
                panes, overview, logs = console_panels(root, Path(folder), lambda: [])
                panes.pack(fill="both", expand=True)
                root.update()
                logs.notebook.select(1)
                root.update()
                self.assertGreaterEqual(logs.frame.winfo_height(), 160)
                self.assertGreater(logs.text.winfo_height(), 60)
                self.assertLessEqual(logs.frame.winfo_y() + logs.frame.winfo_height(), root.winfo_height())
                logs.notebook.select(0)
                root.update()
                self.assertIsNone(overview.selected)
                self.assertFalse(overview.tasks.winfo_ismapped())
            finally:
                root.destroy()
