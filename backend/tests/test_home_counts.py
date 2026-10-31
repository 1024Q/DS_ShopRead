import copy, tempfile, time, unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.business import BusinessService, create_router, options_for
from backend.home_counts import verify
from backend.store import Store

CASES = [
    ("orders", "待发货"),
    ("orders", "即将逾期发货"),
    ("aftersales", "待商家处理"),
    ("aftersales", "待处理即将逾期"),
    ("tickets", "待处理"),
]


def payload(kind, status, number=1, complete=True):
    scope = options_for(kind, status=status)
    day = scope["date_to"]
    row = {"id": "match", "created_at": day + " 12:00:00", "status": status, "name": "商品"}
    if status == "即将逾期发货":
        row.update(status="待发货", attention_status=status)
    if status == "待处理即将逾期":
        row.update(status="待商家处理", progress="待商家处理", attention_status=status)
    if kind == "aftersales":
        row["created_at"] = "2000-01-01 12:00:00"
    others = [dict(row, id="closed", status="已完成", progress="已完成", attention_status="")]
    if kind == "orders":
        others.append(dict(row, id="stale", status="待发货", attention_status="", _order_status_stale=True))
    return {
        "complete": complete,
        "rows": [dict(row, id="match" + str(i)) for i in range(number)] + others,
        "_sync": {"scope": scope, "snapshot_started_at": time.time()},
    }


class VerificationTests(unittest.TestCase):
    def test_complete_urgent_or_old_scope_does_not_verify_full_backlog(self):
        wanted = options_for("tickets", status="待处理")
        source = {"updated_at": "now", "complete": True, "snapshot_scope": {**wanted, "_attention": True}}
        self.assertFalse(verify("tickets", wanted, source)[0])
        source["snapshot_scope"] = {**wanted, "status": "2小时内将逾期"}
        self.assertFalse(verify("tickets", wanted, source)[0])
        source["snapshot_scope"] = {**wanted, "date_to": "2000-01-01"}
        self.assertFalse(verify("tickets", wanted, source)[0])
        source["snapshot_scope"] = wanted
        self.assertTrue(verify("tickets", wanted, source)[0])
        source["complete"] = False
        self.assertFalse(verify("tickets", wanted, source)[0])

    def test_unlimited_aftersales_is_only_verified_for_matching_pdd_shortcut(self):
        wanted = options_for("aftersales", status="待商家处理")
        scope = {**wanted, "date_from": "2000-01-01", "date_to": "2000-01-02"}
        source = {"platform": "pdd", "updated_at": "now", "complete": True, "snapshot_scope": scope}
        self.assertTrue(verify("aftersales", wanted, source)[0])
        source["platform"] = "taobao"
        self.assertFalse(verify("aftersales", wanted, source)[0])


class LocalCountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.store.add("fixture1", "pw", name="同名店")
        self.store.add("fixture2", "pw", name="同名店")
        self.service = BusinessService(self.store, lambda _: SimpleNamespace(state="logged_in"))
        self.service.export_jobs = Mock()
        app = FastAPI()
        app.include_router(create_router(self.service, lambda: None))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_all_five_counts_match_list_filters_and_per_shop_completeness(self):
        for kind, status in CASES:
            for i, shop in enumerate(self.store.list()):
                scope = options_for(kind, status=status)
                self.store.write_cache(
                    shop["id"], self.service.key(kind, scope, shop), payload(kind, status, i + 1, complete=i == 0)
                )
            query = {"status": status, "page_size": 100}
            actual = self.client.get("/api/data/" + kind, params=query)
            counted = self.client.get("/api/data/" + kind, params={**query, "home_counts": True})
            self.assertEqual(counted.status_code, 200, counted.text)
            regular = actual.json()
            counts = counted.json()
            self.assertEqual(counts["total"], regular["total"], (kind, status))
            per_shop = Counter(row["shop_id"] for row in regular["rows"])
            for entry in counts["home_counts"]["rows"]:
                self.assertEqual(entry["count"], per_shop[entry["shop_id"]])
            self.assertEqual([row["verified"] for row in counts["home_counts"]["rows"]], [True, False])
            self.assertEqual(counts["rows"], [])
            self.assertEqual(counts["home_counts"]["filters"]["date_from"], "")
        self.assertFalse(self.service.jobs)

    def test_count_snapshot_metadata_replaces_earlier_data_projection(self):
        shop = self.store.list()[0]
        kind, status = "tickets", "待处理"
        scope = options_for(kind, status=status)
        key = self.service.key(kind, scope, shop)
        self.store.write_cache(shop["id"], key, payload(kind, status, 5))
        original = self.service.data

        def read_then_publish(*args, **kwargs):
            data = original(*args, **kwargs)
            self.store.write_cache(shop["id"], key, payload(kind, status, 1, complete=False))
            return data

        with patch.object(self.service, "data", side_effect=read_then_publish):
            response = self.client.get(
                "/api/data/tickets", params={"status": status, "shop_id": shop["id"], "home_counts": True}
            )
        self.assertEqual(response.status_code, 200, response.text)
        entry = response.json()["home_counts"]["rows"][0]
        self.assertEqual(entry["count"], 1)
        self.assertFalse(entry["verified"])


class CenterCountTests(unittest.TestCase):
    def setUp(self):
        from backend.center.database import Database
        from backend.center.business import CentralBusiness

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database("sqlite:///" + str(Path(self.tmp.name) / "center.db"))
        self.addCleanup(self.db.engine.dispose)
        self.node = self.db.register("fixture")["id"]
        self.db.ingest(
            self.node,
            [
                {
                    "shop": str(i),
                    "scope": "shop_metadata",
                    "updated": time.time(),
                    "payload": {"platform": "pdd", "name": "同名店", "state": "sleeping"},
                }
                for i in (1, 2)
            ],
        )
        self.service = CentralBusiness(self.db)
        self.service.export_jobs = Mock()
        app = FastAPI()
        app.include_router(create_router(self.service, lambda: None))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_counts_match_destination_per_shop_and_never_register_missing_collection(self):
        for kind, status in CASES:
            for i, shop in enumerate(self.db.shops()):
                key = self.service.key(kind, options_for(kind, status=status), shop)
                self.db.ingest(
                    self.node,
                    [
                        {
                            "shop": str(shop["local_id"]),
                            "scope": key,
                            "updated": time.time(),
                            "payload": payload(kind, status, i + 1, complete=i == 0),
                        }
                    ],
                )
            with patch.object(self.service, "request_sync") as collect:
                query = {"status": status, "page_size": 100}
                regular = self.client.get("/api/data/" + kind, params=query).json()
                response = self.client.get("/api/data/" + kind, params={**query, "home_counts": True})
                self.assertEqual(response.status_code, 200, response.text)
                counts = response.json()
                self.assertEqual(counts["total"], regular["total"])
                self.assertEqual(
                    {r["shop_id"]: r["count"] for r in counts["home_counts"]["rows"]},
                    dict(Counter(r["shop_id"] for r in regular["rows"])),
                )
                self.assertEqual([r["verified"] for r in counts["home_counts"]["rows"]], [True, False])
                collect.assert_not_called()

    def test_missing_or_force_count_get_is_read_only_while_manual_sync_still_works(self):
        from backend.center.database import jobs

        for extra in ({}, {"force": True}):
            response = self.client.get("/api/data/tickets", params={"status": "待处理", "home_counts": True, **extra})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(all(not row["verified"] for row in response.json()["home_counts"]["rows"]))
        self.assertEqual(self.db.list(jobs), [])
        response = self.client.post("/api/data/tickets/sync", params={"status": "待处理", "force": True})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(len(self.db.list(jobs)), 2)


if __name__ == "__main__":
    unittest.main()
