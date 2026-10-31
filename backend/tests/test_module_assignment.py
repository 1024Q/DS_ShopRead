import base64, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend import collection
from backend.tests.collection_fixture import assign
from backend.task_catalog import grouped_id
from backend.store import Store
from backend.center.database import Database
from backend.center.server import create_app


class ModuleAssignmentTests(unittest.TestCase):
    def test_concurrency_fanout_single_node_and_partial_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database("sqlite:///" + str(Path(folder) / "center.db"))
            nodes = [db.register("甲"), db.register("乙")]
            app = create_app(db, "x" * 40)
            calls = []
            fail = False
            configs = {}

            async def request(node, method, path, query="", body=b""):
                if method == "PUT":
                    calls.append((node, path, json.loads(body)))
                    configs[node] = json.loads(body)
                denied = fail and node == nodes[1]["id"]
                value = {"detail": "节点离线"} if denied else {"collection": configs[node]}
                return {
                    "status": 503 if denied else 200,
                    "body": base64.b64encode(json.dumps(value).encode()).decode(),
                    "headers": {"content-type": "application/json"},
                }

            try:
                with TestClient(app) as client, patch.object(app.state.hub, "request", side_effect=request):
                    headers = {"Authorization": "Bearer " + "x" * 40}
                    payload = {
                        "max_concurrent": 8,
                        "shop_concurrent": 2,
                    }
                    r = client.put("/api/system/concurrency", headers=headers, json=payload)
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual(r.json()["node_count"], 2)
                    self.assertEqual({c[0] for c in calls}, {n["id"] for n in nodes})
                    self.assertTrue(all(c[2] == payload for c in calls))
                    calls.clear()
                    r = client.put("/api/nodes/1/proxy/system/concurrency", headers=headers, json=payload)
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual(len(calls), 1)
                    fail = True
                    r = client.put("/api/system/concurrency", headers=headers, json=payload)
                    self.assertEqual(r.status_code, 502, r.text)
                    self.assertEqual([v["ok"] for v in r.json()["results"]], [True, False])
            finally:
                db.engine.dispose()

    def test_batch_assignments_are_atomic_and_preserve_other_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            before = collection.settings(store)
            items = [
                {"platform": "pdd", "kind": grouped_id("orders@today"), "tab": "task", "target": "custom2"},
                {"platform": "pdd", "kind": "products@base", "tab": "task", "target": "daily"},
            ]
            saved = collection.save_settings(store, {"module_assignments": items})
            self.assertEqual(saved["task_modes"]["pdd"][items[0]["kind"]], "custom2")
            self.assertEqual(saved["frequency_groups"], before["frequency_groups"])
            with self.assertRaises(ValueError):
                collection.save_settings(
                    store, {"module_assignments": [dict(items[0], target="off"), dict(items[1], target="invalid")]}
                )
            self.assertEqual(collection.settings(store), saved)

    def test_patch_preserves_unmodified_node_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            before = assign(store, {"evidence@base": "custom"})
            c = collection.save_settings(
                store, {"settings_patch": {"group_updates": {"realtime": {"seconds": 120, "name": "快速采集"}}}}
            )
            self.assertEqual(c["task_modes"], before["task_modes"])
            self.assertEqual(
                next(g for g in c["frequency_groups"] if g["id"] == "custom"),
                next(g for g in before["frequency_groups"] if g["id"] == "custom"),
            )
            for payload in ({"attention_seconds": 30}, {"settings_patch": {"platform_modes": {}}}):
                with self.assertRaises(ValueError):
                    collection.save_settings(store, payload)
            self.assertEqual(collection.settings(store), c)

    def test_all_node_differences_and_patch_cache_refresh(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database("sqlite:///" + str(Path(folder) / "center.db"))
            nodes = [db.register("甲"), db.register("乙")]
            app = create_app(db, "x" * 40)
            configs = {n["id"]: {"max_concurrent": 6 + i * 6, "shop_concurrent": 2 + i} for i, n in enumerate(nodes)}
            calls = []

            async def request(node, method, path, query="", body=b""):
                if method == "PUT":
                    payload = json.loads(body)
                    calls.append(payload)
                    if "settings_patch" in payload:
                        configs[node].update(payload["settings_patch"])
                    else:
                        for a in payload["module_assignments"]:
                            field = {"task": "task_modes"}[a["tab"]]
                            configs[node].setdefault(field, {}).setdefault(a["platform"], {})[a["kind"]] = a["target"]
                return {
                    "status": 200,
                    "body": base64.b64encode(json.dumps({"collection": configs[node]}).encode()).decode(),
                    "headers": {"content-type": "application/json"},
                }

            try:
                with TestClient(app) as client, patch.object(app.state.hub, "request", side_effect=request):
                    app.state.hub.peers.update({n["id"]: object() for n in nodes})
                    headers = {"Authorization": "Bearer " + "x" * 40}
                    r = client.get("/api/system/settings", headers=headers)
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual([n["collection"]["max_concurrent"] for n in r.json()["node_configs"]], [6, 12])
                    payload = {"settings_patch": {"max_concurrent": 18}}
                    r = client.put("/api/system/settings", headers=headers, json=payload)
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual(calls, [payload, payload])
                    r = client.get("/api/system/settings", headers=headers)
                    self.assertEqual([n["collection"]["max_concurrent"] for n in r.json()["node_configs"]], [18, 18])
                    self.assertEqual([n["collection"]["shop_concurrent"] for n in r.json()["node_configs"]], [2, 3])
                    batch = {
                        "module_assignments": [
                            {"platform": "pdd", "kind": "orders@recent", "tab": "task", "target": "custom"},
                        ]
                    }
                    r = client.put("/api/system/settings", headers=headers, json=batch)
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual(calls[-2:], [batch, batch])
                    app.state.hub.peers.clear()
            finally:
                db.engine.dispose()

    def test_group_members_share_frequency_but_independent_groups_keep_theirs(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            c = assign(store, {"orders@unpaid": "custom2", "overview@base": "realtime"})
            for status in ("待付款", "待发货", "待收货"):
                self.assertEqual(collection.effective_interval("orders", {"status": status}, c), 600)
            self.assertEqual(collection.effective_interval("evidence", {"_attention": True}, c), 60)
            c = assign(store, {"orders@unpaid": "off"})
            self.assertEqual(collection.mode("orders", {"status": "待收货"}, c), "off")

    def test_all_nodes_assignment_and_partial_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database("sqlite:///" + str(Path(folder) / "center.db"))
            nodes = [db.register("节点甲"), db.register("节点乙")]
            app = create_app(db, "x" * 40)
            calls = []
            fail = False
            configs = {}

            async def request(node, method, path, query="", body=b""):
                if method == "PUT":
                    calls.append(node)
                if fail and node == nodes[1]["id"]:
                    return {
                        "status": 503,
                        "body": base64.b64encode(json.dumps({"detail": "节点离线"}).encode()).decode(),
                        "headers": {"content-type": "application/json"},
                    }
                if method == "PUT":
                    assignment = json.loads(body)["module_assignments"][0]
                    configs[node] = {"task_modes": {assignment["platform"]: {assignment["kind"]: assignment["target"]}}}
                config = configs[node]
                return {
                    "status": 200,
                    "body": base64.b64encode(json.dumps({"collection": config}).encode()).decode(),
                    "headers": {"content-type": "application/json"},
                }

            try:
                with TestClient(app) as client, patch.object(app.state.hub, "request", side_effect=request):
                    payload = {
                        "module_assignments": [
                            {"platform": "pdd", "kind": "orders@recent", "tab": "task", "target": "custom"}
                        ]
                    }
                    r = client.put(
                        "/api/system/settings", headers={"Authorization": "Bearer " + "x" * 40}, json=payload
                    )
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual(r.json()["node_count"], 2)
                    self.assertEqual(set(calls), {n["id"] for n in nodes})
                    fail = True
                    r = client.put(
                        "/api/system/settings", headers={"Authorization": "Bearer " + "x" * 40}, json=payload
                    )
                    self.assertEqual(r.status_code, 502, r.text)
                    self.assertIn("节点乙", r.json()["detail"])
            finally:
                db.engine.dispose()
