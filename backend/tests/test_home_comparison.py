import tempfile, unittest
from datetime import datetime, timedelta
from backend.store import Store
from backend.business_reader import BEIJING
from backend.home_comparison import read
from backend.admission import choose
from backend import collection


class HomeComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.a = self.store.add("one", "pw")
        self.b = self.store.add("two", "pw")
        self.now = datetime.now(BEIJING)
        self.yesterday = self.now - timedelta(days=1)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, sid, metrics, complete=True):
        self.store.write_cache(
            sid,
            "overview:test",
            {"metrics": metrics, "complete": complete, "_sync": {"snapshot_started_at": self.yesterday.timestamp()}},
        )

    def test_comparison_pairs_same_shops_and_keeps_missing_unknown(self):
        self.save(self.a, {"sales": 100, "orders": 0})
        self.save(self.b, {"sales": 200})
        result = read(self.store, self.store.list(), [{"shop_id": self.a, "sales": 150, "orders": 3}], self.now)[
            "fields"
        ]
        self.assertEqual(result["sales"]["value"], 300)
        self.assertEqual(result["sales"]["change"], 50)
        self.assertEqual(result["sales"]["comparable"], 1)
        self.assertEqual(result["orders"]["direction"], "up")
        self.assertIsNone(result["orders"]["change"])
        self.assertIsNone(result["visitors"]["value"])
        self.assertEqual(result["visitors"]["direction"], "unknown")

    def test_scope_partial_and_old_profile_are_excluded(self):
        self.save(self.a, {"sales": 100})
        self.save(self.b, {"sales": 200}, False)
        result = read(self.store, [s for s in self.store.list() if s["id"] == self.b], [], self.now)
        self.assertIsNone(result["fields"]["sales"]["value"])
        with self.store.connect() as db:
            db.execute("UPDATE shops SET profile_version=profile_version+1 WHERE id=?", (self.a,))
        self.assertIsNone(read(self.store, self.store.list(), [], self.now)["fields"]["sales"]["value"])

    def test_previous_day_current_value_is_not_compared(self):
        self.save(self.a, {"sales": 100})
        metric = read(
            self.store, self.store.list(), [{"shop_id": self.a, "sales": 100, "day_pending": True}], self.now
        )["fields"]["sales"]
        self.assertEqual(metric["value"], 100)
        self.assertEqual(metric["comparable"], 0)

    def test_saved_yesterday_report_is_used_without_enqueuing(self):
        from backend.business import BusinessService
        from backend.transactions import options_for

        service = BusinessService(self.store, lambda _: None)
        records = self.store.list()
        keys = {
            s["id"]: (service.key("overview", {}, s), service.key("transactions", options_for("yesterday"), s))
            for s in records
        }
        self.store.write_cache(
            self.a,
            keys[self.a][1],
            {
                "complete": True,
                "metrics": {"summary": {"sales": 200, "orders": 10}},
                "_sync": {"scope": options_for("yesterday")},
            },
        )
        result = read(self.store, records, [{"shop_id": self.a, "sales": 100, "orders": 5}], self.now, keys)["fields"]
        self.assertEqual(result["sales"]["value"], 200)
        self.assertEqual(result["sales"]["label"], "昨日交易")
        self.assertEqual(result["sales"]["change"], -50)
        self.assertEqual(service.jobs, {})
        tomorrow = self.now + timedelta(days=1)
        self.assertIsNone(read(self.store, records, [], tomorrow, keys)["fields"]["sales"]["value"])

    def test_home_admitted_even_when_list_waiting_room_is_full(self):
        config = {**collection.DEFAULTS, "max_concurrent": 4, "realtime_concurrent": 3, "daily_concurrent": 1}
        waiting = {
            (sid, "products"): {"kind": "products", "phase": "queued", "scope": {}, "platform": "pdd"}
            for sid in range(1, 5)
        }
        candidates = [
            ({"id": sid, "platform": "pdd"}, None, "overview", {}, {"last_success": 0}) for sid in range(1, 10)
        ]
        selected, deferred, caps = choose(candidates, waiting, config, self.now.timestamp())
        self.assertEqual(len(selected), caps["normal"] - len(waiting))
        self.assertTrue(deferred)
        self.assertEqual(caps["normal"], 12)
        self.assertEqual(config["max_concurrent"], 4)

    def test_today_promotion_not_starved_by_full_list_queue(self):
        config = {**collection.DEFAULTS, "max_concurrent": 4, "realtime_concurrent": 3, "daily_concurrent": 1}
        waiting = {
            (sid, "orders"): {"kind": "orders", "phase": "queued", "scope": {}, "platform": "pdd"}
            for sid in range(1, 5)
        }
        candidate = ({"id": 8, "platform": "pdd"}, None, "ad_reports", {"trade_range": "realtime"}, {"last_success": 0})
        selected, deferred, _ = choose([candidate], waiting, config, self.now.timestamp())
        self.assertEqual(selected, [candidate])
        self.assertFalse(deferred)
