import hashlib, json, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend.center.database import Database
from backend.center.server import create_app
from backend.center.coverage import project


class CoverageOwnershipTests(unittest.TestCase):
    def test_current_ownership_filters_retired_ids_and_recounts(self):
        shops = [
            {"id": 20, "local_id": 1, "node_id": "new", "platform": "pdd", "name": "moved"},
            {"id": 1, "local_id": 2, "node_id": "old", "platform": "pdd", "name": "kept"},
        ]
        value = {
            "rows": [
                {"shop_id": 1, "total": 9, "updated": 9, "overdue_modules": 8},
                {"shop_id": 2, "total": 9, "updated": 4, "overdue_modules": 2},
            ],
            "total_shops": 99,
            "updated_modules": 99,
        }
        r = project(value, shops, "old", {})
        self.assertEqual([x["shop_id"] for x in r["rows"]], [1])
        self.assertEqual(r["total_shops"], 1)
        self.assertEqual(r["updated_modules"], 4)
        self.assertEqual(r["overdue_modules"], 2)
        self.assertEqual(project(value, shops, "old", {"shop_id": "20"})["rows"], [])
        self.assertEqual(project(value, shops, "old", {"platform": "taobao"})["rows"], [])
        r = project({"rows": []}, shops, "new", {})
        self.assertEqual(len(r["rows"]), 1)
        self.assertEqual(r["excluded_shops"], 1)
        self.assertEqual(r["updated_modules"], 0)

    def test_migrated_shop_cached_reports_single_node_and_all_nodes(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database("sqlite:///" + str(Path(folder) / "db.sqlite"))
            a, b = db.register("old"), db.register("new")
            meta = {"platform": "pdd", "name": "shop", "state": "logged_in"}
            db.ingest(a["id"], [{"shop": "1", "scope": "shop_metadata", "updated": 1, "payload": meta}])
            shop = db.shops()[0]
            db.move_shop(shop, b["id"], 7, meta)
            key = "response:" + hashlib.sha256(b"home/progress?").hexdigest()
            for node, local in ((a["id"], 1), (b["id"], 7)):
                data = {
                    "rows": [
                        {
                            "shop_id": local,
                            "total": 9,
                            "updated": 8,
                            "daily_total": 1,
                            "daily_updated": 1,
                            "overdue_modules": 2,
                            "modules": [],
                        }
                    ],
                    "total_shops": 1,
                    "updated_modules": 8,
                    "total_modules": 9,
                }
                db.ingest(node, [{"shop": "0", "scope": key, "updated": time.time(), "payload": {"value": data}}])
            with (
                patch("backend.center.report_exports.ROOT", Path(folder) / "exports"),
                TestClient(create_app(db, "a" * 40)) as client,
            ):
                h = {"Authorization": "Bearer " + "a" * 40}
                r = client.get("/api/home/progress", headers=h)
                self.assertEqual(r.status_code, 200, r.text)
                data = r.json()
                self.assertEqual(len(data["rows"]), 1)
                self.assertEqual(data["rows"][0]["shop_id"], shop["id"])
                self.assertEqual(data["total_modules"], 9)
                self.assertEqual(data["overdue_modules"], 2)
                old = next(n["id"] for n in db.registry() if n["node"] == a["id"])
                data = client.get(f"/api/nodes/{old}/proxy/home/progress", headers=h).json()
                self.assertEqual(data["rows"], [])
                self.assertEqual(data["total_shops"], 0)
            db.engine.dispose()
