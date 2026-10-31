import json, tempfile, time, unittest
from backend.store import Store
from backend.payment_daily import initialize


class PaymentDailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("one", "pw")

    def payload(self, scope="yesterday", started=100, **summary):
        return {
            "rows": [],
            "complete": True,
            "metrics": {
                "summary": {"sales": 100.15, "refund_amount": 20.09, "orders": 10, "refund_orders": 3, **summary}
            },
            "_sync": {"scope": {"trade_range": scope, "day": "2026-10-11"}, "snapshot_started_at": started},
        }

    def records(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM payment_daily ORDER BY profile_version")]

    def test_exact_net_and_yesterday_only(self):
        self.store.write_cache(self.sid, "transactions:live", self.payload("realtime"))
        self.store.write_cache(self.sid, "transactions:week", self.payload("week"))
        self.assertEqual(self.records(), [])
        self.store.write_cache(self.sid, "transactions:yesterday", self.payload())
        row = self.records()[0]
        self.assertEqual((row["stat_date"], row["net_payment_cents"], row["net_orders"]), ("2026-10-10", 8006, 7))
        self.assertEqual(row["complete"], 1)
        self.assertEqual(json.loads(row["source_summary"])["refund_amount"], 20.09)

    def test_missing_is_not_zero_and_negative_net_is_not_clamped(self):
        self.store.write_cache(self.sid, "transactions:missing", self.payload(refund_amount=None, refund_orders=None))
        self.assertIsNone(self.records()[0]["net_payment_cents"])
        self.assertIsNone(self.records()[0]["net_orders"])
        self.assertEqual(self.records()[0]["complete"], 0)
        self.store.write_cache(
            self.sid,
            "transactions:full",
            self.payload(started=200, sales=1, refund_amount=3, orders=1, refund_orders=4),
        )
        self.assertEqual((self.records()[0]["net_payment_cents"], self.records()[0]["net_orders"]), (-200, -3))

    def test_old_or_incomplete_never_replaces_valid_record(self):
        self.store.write_cache(self.sid, "transactions:full", self.payload(started=200))
        self.store.write_cache(self.sid, "transactions:older", self.payload(started=100, sales=5))
        self.store.write_cache(self.sid, "transactions:missing", self.payload(started=300, refund_amount=None))
        self.assertEqual(self.records()[0]["net_payment_cents"], 8006)
        with self.store.connect() as db:
            db.execute("UPDATE shops SET profile_version=1 WHERE id=?", (self.sid,))
        self.store.write_cache(self.sid, "transactions:new-profile", self.payload(started=400, sales=200.15))
        self.assertEqual(len(self.records()), 2)

    def test_backfill_and_restart_do_not_duplicate(self):
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO daily_archive VALUES(?,?,?,?)",
                (self.sid, "transactions:archived", json.dumps(self.payload()), time.time()),
            )
            db.execute("DELETE FROM settings WHERE key='payment_daily_v1'")
        initialize(self.store)
        initialize(self.store)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["net_orders"], 7)

    def trend(self, started=500, **values):
        day = lambda d, **v: {
            "id": "dayList:" + d,
            "series": "每日",
            "date": d,
            "sales": 10.5,
            "refund_amount": 0.5,
            "orders": 3,
            "refund_orders": 1,
            **v,
        }
        rows = [
            day("2026-10-08", **values),
            day("2026-10-09", **values),
            day("2026-10-10", **values),
            {"id": "today", "series": "今日累计", "date": "2026-10-11", "sales": 1},
        ]
        return {
            "rows": rows,
            "complete": True,
            "metrics": {"summary": {}},
            "_sync": {"scope": {"trade_range": "month", "day": "2026-10-11"}, "snapshot_started_at": started},
        }

    def test_trend_fills_only_missing_days_and_never_replaces_yesterday_report(self):
        self.store.write_cache(self.sid, "transactions:yesterday", self.payload())
        self.store.write_cache(self.sid, "transactions:month", self.trend())
        rows = {r["stat_date"]: r for r in self.records()}
        self.assertEqual(sorted(rows), ["2026-10-08", "2026-10-09", "2026-10-10"])
        self.assertEqual(rows["2026-10-10"]["net_payment_cents"], 8006)
        self.assertEqual((rows["2026-10-08"]["net_payment_cents"], rows["2026-10-08"]["complete"]), (1000, 1))
        self.assertEqual(json.loads(rows["2026-10-08"]["source_summary"])["basis"], "daily_trend")
        self.store.write_cache(self.sid, "transactions:month2", self.trend(started=600, sales=20))
        self.assertEqual(
            {r["stat_date"]: r["sales_cents"] for r in self.records()},
            {"2026-10-08": 2000, "2026-10-09": 2000, "2026-10-10": 10015},
        )

    def test_missing_days_starts_at_first_record(self):
        from datetime import date
        from backend.payment_daily import missing_days

        with self.store.connect() as db:
            self.assertEqual(missing_days(db, self.sid, date(2026, 10, 13)), [])
        self.store.write_cache(self.sid, "transactions:yesterday", self.payload())
        with self.store.connect() as db:
            self.assertEqual(missing_days(db, self.sid, date(2026, 10, 12)), [])
            self.assertEqual(missing_days(db, self.sid, date(2026, 10, 14)), ["2026-10-11", "2026-10-12"])
