import base64, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend.center.configuration import aggregate_status
from backend.center.database import Database
from backend.center.server import create_app


class NodeConfigurationTests(unittest.TestCase):
    def test_runtime_sums_and_policy_differences(self):
        a = {
            "collection": {"max_concurrent": 8, "realtime_concurrent": 6, "daily_concurrent": 2, "shop_concurrent": 2},
            "queues": {
                "max_concurrent": 8,
                "focus": {"running": 5, "reserved": 6},
                "normal": {"running": 2, "reserved": 2},
            },
        }
        b = {
            "collection": {"max_concurrent": 6, "realtime_concurrent": 2, "daily_concurrent": 4, "shop_concurrent": 2},
            "queues": {
                "max_concurrent": 6,
                "focus": {"running": 2, "reserved": 2},
                "normal": {"running": 3, "reserved": 4},
            },
        }
        result = aggregate_status({}, [a, b])
        self.assertEqual(result["queues"]["max_concurrent"], 14)
        self.assertEqual(result["queues"]["focus"]["reserved"], 8)
        self.assertEqual(result["queues"]["normal"]["running"], 5)
        self.assertIsNone(result["collection"]["max_concurrent"])
        self.assertEqual(result["collection"]["shop_concurrent"], 2)

    def test_live_reads_partial_patches_and_readback_mismatch(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database("sqlite:///" + str(Path(folder) / "center.db"))
            nodes = [db.register("甲"), db.register("乙")]
            app = create_app(db, "x" * 40)
            configs = {n["id"]: {"max_concurrent": 4 + i * 2, "shop_concurrent": 2} for i, n in enumerate(nodes)}
            writes = []
            lie = False
            offline = False

            async def request(node, method, path, query="", body=b""):
                if offline and node == nodes[1]["id"]:
                    return {
                        "status": 503,
                        "body": base64.b64encode(b"{}").decode(),
                        "headers": {"content-type": "application/json"},
                    }
                if method == "PUT":
                    changes = json.loads(body)
                    writes.append((node, changes))
                    if not lie:
                        configs[node].update(changes)
                    data = {**configs[node], **changes}  # A misleading PUT response must not pass verification.
                else:
                    data = configs[node]
                return {
                    "status": 200,
                    "body": base64.b64encode(json.dumps({"collection": data}).encode()).decode(),
                    "headers": {"content-type": "application/json"},
                }

            try:
                with TestClient(app) as client, patch.object(app.state.hub, "request", side_effect=request):
                    headers = {"Authorization": "Bearer " + "x" * 40}
                    first = client.get("/api/system/settings", headers=headers).json()
                    self.assertEqual(first["scope_node_id"], 0)
                    configs[nodes[0]["id"]]["max_concurrent"] = 10
                    second = client.get("/api/system/settings", headers=headers).json()
                    self.assertEqual(second["node_configs"][0]["collection"]["max_concurrent"], 10)
                    result = client.put("/api/system/concurrency", headers=headers, json={"shop_concurrent": 3})
                    self.assertEqual(result.status_code, 200, result.text)
                    self.assertEqual(writes, [(n["id"], {"shop_concurrent": 3}) for n in nodes])
                    self.assertEqual([c["max_concurrent"] for c in configs.values()], [10, 6])
                    writes.clear()
                    result = client.put(
                        "/api/nodes/1/proxy/system/concurrency", headers=headers, json={"shop_concurrent": 1}
                    )
                    self.assertEqual(result.status_code, 200, result.text)
                    self.assertEqual(writes, [(nodes[0]["id"], {"shop_concurrent": 1})])
                    self.assertEqual(result.json()["results"], [{"node": "甲", "ok": True}])
                    lie = True
                    result = client.put("/api/system/concurrency", headers=headers, json={"shop_concurrent": 2})
                    self.assertEqual(result.status_code, 502, result.text)
                    self.assertTrue(all(not r["ok"] for r in result.json()["results"]))
                    offline = True
                    result = client.get("/api/system/settings", headers=headers).json()
                    self.assertNotIn("collection", result["node_configs"][1])
            finally:
                db.engine.dispose()

    def test_local_partial_concurrency_preserves_other_fields(self):
        from backend import main, collection
        from backend.business import BusinessService
        from backend.store import Store

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            service = BusinessService(store, None)
            with patch.object(main, "store", store), patch.object(main, "business", service):
                client = TestClient(main.app)
                headers = {"Authorization": "Bearer " + main.TOKEN}
                result = client.put("/api/system/concurrency", headers=headers, json={"shop_concurrent": 3})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["collection"]["max_concurrent"], 20)
                self.assertEqual(result.json()["collection"]["shop_concurrent"], 3)
                before = collection.settings(store)
                bad = client.put("/api/system/concurrency", headers=headers, json={"max_concurrent": 0})
                self.assertEqual(bad.status_code, 400)
                self.assertEqual(collection.settings(store), before)
