import tempfile
import unittest
from datetime import datetime, timedelta
from backend import collection
from backend.business import BusinessService, options_for
from backend.business_reader import BEIJING
from backend.browser import Session
from backend.store import Store


class MidnightTests(unittest.TestCase):
    def test_home_due_at_midnight_without_changing_package_or_daily_policy(self):
        before = datetime(2026, 12, 31, 23, 59, 59, tzinfo=BEIJING).timestamp()
        after = before + 1
        config = dict(collection.DEFAULTS)
        for kind in ("overview", "transactions", "ad_reports"):
            self.assertTrue(collection.due(kind, {}, before, config, after))
            self.assertFalse(collection.due(kind, {}, after, config, after + 1))
            self.assertFalse(collection.due(kind, {}, before, {**config, "module_modes": {kind: "off"}}, after))
        self.assertFalse(collection.due("packages", {}, before, config, after))
        self.assertFalse(collection.due("products", {}, before, config, after))
        self.assertFalse(collection.due("transactions", {"trade_range": "yesterday"}, before, config, after))

    def test_old_overview_not_counted_as_today_and_history_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("fixture", "pw")
            record = store.list()[0]
            session = Session(record, store)
            service = BusinessService(store, lambda _: session)
            opts = options_for("overview")
            key = service.key("overview", opts, record)
            old = (datetime.now(BEIJING) - timedelta(days=1)).timestamp()
            payload = {
                "metrics": {"sales": 999, "orders": 9, "pending_shipping": 4},
                "rows": [],
                "complete": True,
                "_sync": {"snapshot_started_at": old},
            }
            store.write_cache(sid, key, payload)
            data = service.data("overview", opts)
            self.assertIsNone(data["totals"]["sales"])
            self.assertIsNone(data["totals"]["orders"])
            self.assertEqual(data["totals"]["pending_shipping"], 4)
            self.assertTrue(data["sources"][0]["day_pending"])
            self.assertEqual(store.read_cache(sid, key)["payload"]["metrics"]["sales"], 999)
            payload["_sync"]["snapshot_started_at"] = datetime.now(BEIJING).timestamp()
            payload["metrics"]["sales"] = 0
            store.write_cache(sid, key, payload)
            data = service.data("overview", opts)
            self.assertEqual(data["totals"]["sales"], 0)
            self.assertFalse(data["sources"][0]["day_pending"])
