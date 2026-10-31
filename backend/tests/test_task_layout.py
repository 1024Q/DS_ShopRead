import copy
import tempfile
import unittest
from datetime import datetime, timedelta

from backend import collection
from backend.business_reader import BEIJING
from backend.center.configuration import matches_patch
from backend.frequency_policy import migrate, selected, step_catalog
from backend.home_only_pause import MARKER, REVISION
from backend.platforms import PLATFORMS
from backend.store import Store
from backend.task_catalog import GROUPS, expand_legacy
from backend.task_layout import defaults, resolve


def scope_for(identifier):
    kind, category = identifier.split("@")
    if category == "attention":
        return {"_attention": True}
    if kind == "orders":
        if category in ("today", "yesterday"):
            return {
                "_order_lane": "recent",
                "date_from": (datetime.now(BEIJING).date() - timedelta(days=category == "yesterday")).isoformat(),
            }
        if category == "recheck":
            return {"_order_lane": "recheck"}
        return {
            "status": {
                "unpaid": "待付款",
                "unshipped": "待发货",
                "unreceived": "待收货",
                "shipping_due": "即将逾期发货",
                "refund": "退款成功",
            }[category]
        }
    if kind == "aftersales" and category in ("merchant_pending", "expiring"):
        return {"status": "待商家处理" if category == "merchant_pending" else "待处理即将逾期"}
    if kind == "tickets" and category == "pending":
        return {"status": "待处理"}
    if category == "details":
        return {"_invoice_details_refresh": True}
    return {"trade_range": category} if category in ("yesterday", "week", "month") else {}


class TaskLayoutTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(directory.name)
        self.config = collection.settings(self.store)

    def layout_save(self, patch):
        return collection.save_settings(self.store, {"settings_patch": {"task_layout_patch": patch}})

    def test_every_supported_step_has_one_default_parent(self):
        layout = defaults()
        self.assertEqual(len(layout["groups"]), 12)
        for step in step_catalog():
            for platform in PLATFORMS:
                self.assertEqual(step["id"] in layout["assignments"][platform], platform in step["platforms"])
                if platform in step["platforms"]:
                    parent = resolve({"task_layout": layout}, platform, step["id"])
                    self.assertIn(parent, layout["groups"])
        self.assertEqual(resolve(self.config, "pdd", "products@base")["id"], "product_catalog")
        self.assertEqual(resolve(self.config, "pdd", "orders@shipping_due")["id"], "home_todos")

    def test_migration_preserves_all_effective_leaf_modes_for_all_platforms(self):
        old = copy.deepcopy(self.config)
        old.pop("task_layout")
        old.update(max_concurrent=7, shop_concurrent=2, browser_idle_seconds=31, home_paused=True)
        old[MARKER] = REVISION
        old["task_modes"] = {}
        for number, platform in enumerate(PLATFORMS):
            assignments = {s["id"]: "custom2" for s in step_catalog()}
            assignments.update({k: ("off", "realtime", "daily")[number % 3] for k in GROUPS})
            assignments["ad_reports@history"] = "custom3"
            old["task_modes"][platform] = assignments
        old = expand_legacy(old)
        original = copy.deepcopy(old)
        expected = {
            (p, s["id"]): selected(s["kind"], scope_for(s["id"]), old, p) for p in PLATFORMS for s in step_catalog()
        }
        new = migrate(old)
        self.assertEqual(old, original)
        actual = {
            (p, s["id"]): selected(s["kind"], scope_for(s["id"]), new, p) for p in PLATFORMS for s in step_catalog()
        }
        self.assertEqual(actual, expected)
        for field in (
            "frequency_groups",
            "max_concurrent",
            "shop_concurrent",
            "shop_limits",
            "browser_idle_seconds",
            "max_active_shops",
            "home_paused",
            "platform_modes",
        ):
            self.assertEqual(new[field], old[field])
        self.assertEqual(migrate(new), new)

    def test_organization_edit_does_not_change_any_frequency_or_execution_setting(self):
        saved = self.layout_save(
            {
                "group_updates": {"mine": {"name": "自定义首页任务"}},
                "step_assignments": [{"platform": "pdd", "step_id": "overview@base", "group_id": "mine"}],
            }
        )
        for key in self.config:
            if key != "task_layout":
                self.assertEqual(saved[key], self.config[key], key)
        self.assertEqual(resolve(saved, "pdd", "overview@base"), {"id": "mine", "name": "自定义首页任务"})
        self.assertEqual(collection.settings(self.store), saved)

    def test_step_frequency_is_independent_and_survives_reload(self):
        before = selected("transactions", {}, self.config, "pdd")
        payload = {
            "module_assignments": [{"platform": "pdd", "kind": "overview@base", "tab": "step", "target": "custom2"}]
        }
        saved = collection.save_settings(self.store, payload)
        self.assertEqual(selected("overview", {}, saved, "pdd"), "custom2")
        self.assertEqual(selected("transactions", {}, saved, "pdd"), before)
        self.assertEqual(saved["task_layout"], self.config["task_layout"])
        self.assertEqual(collection.settings(self.store), saved)
        self.assertTrue(matches_patch("system/settings", payload, saved))

    def test_nonempty_delete_requires_transfer_all_platforms(self):
        with self.assertRaisesRegex(ValueError, "仍有子步骤"):
            self.layout_save({"group_updates": {"home_metrics": None}})
        self.assertEqual(collection.settings(self.store), self.config)
        patch = {"group_updates": {"home_metrics": None}, "group_moves": {"home_metrics": "home_todos"}}
        saved = self.layout_save(patch)
        for assignments in saved["task_layout"]["assignments"].values():
            self.assertNotIn("home_metrics", assignments.values())
        self.assertEqual(saved["task_modes"], self.config["task_modes"])
        self.assertTrue(matches_patch("system/settings", {"settings_patch": {"task_layout_patch": patch}}, saved))

    def test_combined_layout_and_frequency_patch_is_atomic(self):
        payload = {
            "settings_patch": {
                "task_layout_patch": {
                    "group_updates": {"mine": {"name": "自定义任务"}},
                    "step_assignments": [{"platform": "pdd", "step_id": "overview@base", "group_id": "mine"}],
                }
            },
            "module_assignments": [{"platform": "pdd", "kind": "overview@base", "tab": "step", "target": "custom2"}],
        }
        saved = collection.save_settings(self.store, payload)
        self.assertTrue(matches_patch("system/settings", payload, saved))
        wrong_mode = copy.deepcopy(saved)
        wrong_mode["task_modes"]["pdd"]["overview@base"] = "off"
        self.assertFalse(matches_patch("system/settings", payload, wrong_mode))
        wrong_parent = copy.deepcopy(saved)
        wrong_parent["task_layout"]["assignments"]["pdd"]["overview@base"] = "home_metrics"
        self.assertFalse(matches_patch("system/settings", payload, wrong_parent))
        payload["module_assignments"][0]["target"] = "daily"
        payload["settings_patch"]["task_layout_patch"]["step_assignments"][0]["group_id"] = "missing"
        with self.assertRaises(ValueError):
            collection.save_settings(self.store, payload)
        self.assertEqual(collection.settings(self.store), saved)

    def test_invalid_drafts_do_not_write(self):
        for patch in (
            {"group_updates": {"bad/id": {"name": "错误"}}},
            {"group_updates": {"mine": {"name": "首页经营指标"}}},
            {"group_updates": {"mine": {"seconds": 20, "name": "频率不能写入父任务"}}},
            {"step_assignments": [{"platform": "jd", "step_id": "overview@base", "group_id": "home_metrics"}]},
            {"step_assignments": [{"platform": "pdd", "step_id": "orders@history", "group_id": "orders_recent"}]},
            {"step_assignments": [{"platform": "pdd", "step_id": "overview@base", "group_id": "home_todos"}] * 2},
            {"group_updates": {"home_metrics": None}, "group_moves": {"home_metrics": "missing"}},
        ):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.layout_save(patch)
            self.assertEqual(collection.settings(self.store), self.config)

    def test_partial_edit_preserves_another_machine_local_configuration(self):
        local = self.layout_save({"group_updates": {"local": {"name": "本机任务"}}})
        patch = {"group_updates": {"home_metrics": {"name": "首页指标"}}}
        saved = self.layout_save(patch)
        self.assertIn({"id": "local", "name": "本机任务"}, saved["task_layout"]["groups"])
        self.assertEqual(saved["task_modes"], local["task_modes"])

    def test_fleet_combined_patch_and_node_confirmation(self):
        import base64
        import json
        from contextlib import ExitStack
        from pathlib import Path
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from backend.center.database import Database
        from backend.center.server import create_app

        with tempfile.TemporaryDirectory() as folder, ExitStack() as cleanup:
            db = Database("sqlite:///" + str(Path(folder) / "center.db"))
            cleanup.callback(db.engine.dispose)
            nodes = [db.register("甲"), db.register("乙")]
            stores = {n["id"]: Store(Path(folder) / str(i)) for i, n in enumerate(nodes)}
            for i, store in enumerate(stores.values()):
                collection.save_settings(store, {"settings_patch": {"max_concurrent": 10 + i}})
            app = create_app(db, "x" * 40)
            calls = []
            reject_second = False

            async def request(node, method, path, query="", body=b""):
                if method == "PUT":
                    value = json.loads(body)
                    calls.append((node, value))
                    if not reject_second or node != nodes[1]["id"]:
                        collection.save_settings(stores[node], value)
                payload = {"collection": collection.settings(stores[node])}
                return {
                    "status": 200,
                    "body": base64.b64encode(json.dumps(payload).encode()).decode(),
                    "headers": {"content-type": "application/json"},
                }

            payload = {
                "settings_patch": {"task_layout_patch": {"group_updates": {"mine": {"name": "测试任务"}}}},
                "module_assignments": [
                    {"platform": "pdd", "kind": "overview@base", "tab": "step", "target": "custom2"}
                ],
            }
            with TestClient(app) as client, patch.object(app.state.hub, "request", side_effect=request):
                headers = {"Authorization": "Bearer " + "x" * 40}
                response = client.put("/api/system/settings", headers=headers, json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual([item[1] for item in calls], [payload, payload])
                self.assertEqual([collection.settings(s)["max_concurrent"] for s in stores.values()], [10, 11])
                reject_second = True
                payload["settings_patch"]["task_layout_patch"]["group_updates"]["mine"]["name"] = "新任务名称"
                response = client.put("/api/system/settings", headers=headers, json=payload)
                self.assertEqual(response.status_code, 502, response.text)
                self.assertEqual([item["ok"] for item in response.json()["results"]], [True, False])

    def test_offline_cache_is_read_only_normalized_and_never_confirms_save(self):
        import base64
        from contextlib import ExitStack
        from pathlib import Path
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from backend.center.database import Database, snapshots
        from backend.center.server import create_app

        with tempfile.TemporaryDirectory() as folder, ExitStack() as cleanup:
            db = Database("sqlite:///" + str(Path(folder) / "center.db"))
            cleanup.callback(db.engine.dispose)
            node = db.register("停止中的节点")
            source = copy.deepcopy(self.config)
            source.pop("task_layout")
            source.pop("task_step_catalog")
            db.ingest(
                node["id"],
                [{"shop": "0", "scope": "collector_status", "updated": 1234, "payload": {"collection": source}}],
            )
            original = db.list(snapshots, node=node["id"], scope="collector_status")
            app = create_app(db, "x" * 40)

            async def unavailable(*args, **kwargs):
                return {
                    "status": 503,
                    "body": base64.b64encode(b"{}").decode(),
                    "headers": {"content-type": "application/json"},
                }

            with TestClient(app) as client, patch.object(app.state.hub, "request", side_effect=unavailable):
                headers = {"Authorization": "Bearer " + "x" * 40}
                response = client.get("/api/system/settings", headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                preview = response.json()
                self.assertTrue(preview["configuration_read_only"])
                self.assertEqual(preview["configuration_source"], "cached")
                self.assertEqual(len(preview["collection"]["task_layout"]["groups"]), 12)
                self.assertTrue(preview["collection"]["task_step_catalog"])
                self.assertFalse(preview["node_configs"][0]["online"])
                self.assertEqual(preview["collection"]["frequency_groups"], source["frequency_groups"])
                response = client.put(
                    "/api/system/settings",
                    headers=headers,
                    json={"settings_patch": {"task_layout_patch": {"group_updates": {"mine": {"name": "不可保存"}}}}},
                )
                self.assertIn(response.status_code, (502, 503), response.text)
                self.assertFalse(response.json()["results"][0]["ok"])
                self.assertEqual(db.list(snapshots, node=node["id"], scope="collector_status"), original)
