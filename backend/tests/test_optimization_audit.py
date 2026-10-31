import asyncio, json, tempfile, time, unittest
from types import SimpleNamespace
from backend.store import Store
from backend import attention, operations_insights, refund_index
from backend.collection_checkpoint import Checkpoint, clear
from backend.business import BusinessService


class Optimizations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("audit", "pw")

    def tearDown(self):
        self.tmp.cleanup()

    def test_targeted_refund_scan_stops_without_claiming_full_list(self):
        from unittest.mock import AsyncMock, patch
        from backend.collection_checkpoint import context
        from backend.operations import paginate

        async def run():
            reader = AsyncMock(return_value={"items": [{"case": "wanted"}], "total": 100})
            with (
                context(self.store, self.sid, "targeted", {}),
                patch("backend.operations.read", reader),
                patch("backend.operations.normalize_operation", return_value=[{"id": "wanted", "amount": 1}]),
                patch("backend.operations.report"),
            ):
                value = await paginate(
                    SimpleNamespace(), "aftersales", {"page": 1}, "page", "items", required_ids={"wanted"}
                )
            self.assertFalse(value["complete"])
            self.assertTrue(value["matched_scope"])
            self.assertEqual(value["source_loaded"], 1)
            self.assertEqual(reader.await_count, 1)  # one matching page; do not fetch it twice

        asyncio.run(run())

    def test_checkpoint_migration_incremental_and_cleanup(self):
        part = {
            "rows": {"one": {"id": "one", "amount": 1}},
            "ids": ["one"],
            "loaded": 1,
            "total": 2,
            "next": 2,
            "done": False,
            "head": "h",
            "tail": "t",
        }
        old = {"started_at": 100, "parts": {"a": part}}
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO collection_checkpoints VALUES(?,?,?,?,?,?)", (self.sid, "test", json.dumps(old), 100, 1, 2)
            )
        c = Checkpoint(self.store, self.sid, "test", {})
        c.save()
        with self.store.connect() as db:
            data = json.loads(db.execute("SELECT payload FROM collection_checkpoints").fetchone()[0])
            self.assertNotIn("rows", data["parts"]["a"])
        c = Checkpoint(self.store, self.sid, "test", {})
        self.assertEqual(c.data["parts"]["a"]["rows"], part["rows"])
        c.data["parts"]["a"]["rows"]["two"] = {"id": "two", "amount": 2}
        c.data["parts"]["a"]["ids"].append("two")
        c.mark("a", [{"id": "two"}])
        c.save()
        c = Checkpoint(self.store, self.sid, "test", {})
        self.assertEqual(set(c.data["parts"]["a"]["rows"]), {"one", "two"})
        c.save()
        self.assertEqual(len(Checkpoint(self.store, self.sid, "test", {}).data["parts"]["a"]["ids"]), 2)
        clear(self.store, self.sid, "test")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM checkpoint_rows").fetchone()[0], 0)

    def test_refund_index_requires_exact_identity_fresh_final_value(self):
        now = time.time()
        r = {"id": "case", "order_number": "order", "status": "退款成功", "amount": 1.25}
        with self.store.connect() as db:
            refund_index.save(db, self.sid, [r], now)
        orders = [{"aftersale_id": "case", "order_number": "order"}]
        self.assertEqual(refund_index.lookup(self.store, self.sid, orders, now)["case"]["amount"], 1.25)
        self.assertEqual(refund_index.lookup(self.store, self.sid, orders, now + 901), {})
        self.assertEqual(
            refund_index.lookup(self.store, self.sid, [{"aftersale_id": "case", "order_number": "another"}], now), {}
        )
        self.assertEqual(refund_index.lookup(self.store, self.sid + 1, orders, now), {})
        with self.store.connect() as db:
            refund_index.save(db, self.sid, [{**r, "amount": 9}], now - 10)
        self.assertEqual(refund_index.lookup(self.store, self.sid, orders, now)["case"]["amount"], 1.25)

    def test_attention_sort_before_pagination(self):
        service = SimpleNamespace(store=self.store, jobs={}, key=lambda *args: "test")
        rows = [
            {"id": str(i), "order_number": str(i), "status": "待举证", "deadline": f"2026-11-{i + 1:02d}"}
            for i in range(25)
        ]
        attention.publish(self.store, self.sid, "evidence", {"rows": rows, "complete": True})
        d = attention.read(service, kind="evidence", sort_by="deadline", sort_direction="desc")
        self.assertEqual(d["rows"][0]["id"], "24")
        self.assertEqual(len(d["rows"]), 20)
        second = attention.read(service, kind="evidence", page=2, sort_by="deadline", sort_direction="desc")
        self.assertEqual(second["rows"][-1]["id"], "0")

    def test_history_sort_before_pagination(self):
        with self.store.connect() as db:
            for i in range(60):
                db.execute(
                    "INSERT INTO attention_changes(shop_id,kind,record_id,change,context,created) VALUES(?,?,?,?,?,?)",
                    (self.sid, "evidence", str(i), "new", json.dumps({"order_number": f"{i:03d}"}), i),
                )
        d = operations_insights.notification_history(self.store, sort_by="order_number", sort_direction="asc")
        self.assertEqual(d["rows"][0]["record_id"], "0")
        self.assertEqual(d["page_count"], 2)
        d = operations_insights.notification_history(self.store, page=2, sort_by="order_number", sort_direction="asc")
        self.assertEqual(d["rows"][0]["record_id"], "50")
