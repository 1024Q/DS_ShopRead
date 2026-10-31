import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.business import options_for
from backend.center.business import CentralBusiness
from backend.center.database import Database
from backend.center.order_api import Changes, ingest


class CenterHomeCountCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database("sqlite:///" + str(Path(self.temp.name) / "center.sqlite"))
        self.node = self.db.register("count-test")["id"]
        self.db.ingest(
            self.node,
            [
                {
                    "shop": "1",
                    "scope": "shop_metadata",
                    "updated": 1,
                    "payload": {"name": "fixture", "platform": "pdd", "state": "logged_in"},
                }
            ],
        )
        self.service = CentralBusiness(self.db)
        self.shop = self.db.shops()[0]
        self.options = options_for("orders", status="待发货")
        self.key = self.service.key("orders", self.options, self.shop)
        self.filters = {k: self.options[k] for k in ("status", "date_from", "date_to")}

    def tearDown(self):
        self.db.engine.dispose()
        self.temp.cleanup()

    def read(self):
        return self.service.home_counts("orders", self.options, filters=self.filters, page_size=1)

    def row(self, identifier, status="待发货"):
        return {"id": identifier, "status": status, "created_at": self.options["date_to"] + " 01:00:00"}

    def save(self, payload, stamp):
        self.db.ingest(self.node, [{"shop": "1", "scope": self.key, "updated": stamp, "payload": payload}])

    def test_snapshot_invalidation_ignores_unrelated_kind_and_returns_isolated_counts(self):
        payload = {
            "rows": [self.row("one"), self.row("sent", "已发货，待收货")],
            "complete": True,
            "_sync": {"scope": self.options},
        }
        self.save(payload, time.time())
        with (
            patch.object(self.service, "data", wraps=self.service.data) as load,
            patch.object(self.service, "request_sync", side_effect=AssertionError("count read collected")),
        ):
            first = self.read()
            self.assertEqual((first["total"], first["rows"]), (1, []))
            first["sources"][0]["complete"] = False
            self.db.ingest(
                self.node,
                [
                    {
                        "shop": "1",
                        "scope": "overview:unrelated",
                        "updated": time.time(),
                        "payload": {"metrics": {"sales": 1}, "complete": True},
                    }
                ],
            )
            again = self.read()
            self.assertTrue(again["sources"][0]["complete"])
            self.assertEqual(load.call_count, 1)
            self.save({**payload, "rows": [self.row("one"), self.row("two")]}, time.time())
            self.assertEqual(self.read()["total"], 2)
            self.assertEqual(load.call_count, 2)

    def test_v2_order_change_clock_invalidates_without_new_metadata(self):
        stream = "a" * 32
        self.save(
            {
                "_order_rows_v2": True,
                "_order_stream": stream,
                "_order_revision": 1,
                "_order_days": {self.options["date_to"]: time.time()},
                "complete": False,
            },
            time.time(),
        )

        def change(revision, row):
            ingest(
                self.db,
                self.node,
                Changes(
                    stream=stream,
                    rows=[
                        {
                            "revision": revision,
                            "shop": "1",
                            "scope": self.key,
                            "id": row["id"],
                            "payload": row,
                            "observed": time.time(),
                        }
                    ],
                ),
            )

        change(1, self.row("one"))
        with patch.object(self.service, "data", wraps=self.service.data) as load:
            self.assertEqual(self.read()["total"], 1)
            self.assertEqual(self.read()["total"], 1)
            self.assertEqual(load.call_count, 1)
            change(2, self.row("two"))
            self.assertEqual(self.read()["total"], 2)
            self.assertEqual(load.call_count, 2)


if __name__ == "__main__":
    unittest.main()
