import copy
import tempfile
import unittest
from backend import collection
from backend.frequency_policy import migrate, group
from backend.history_frequency import recommend, historical
from backend.store import Store


class HistoryFrequencyTests(unittest.TestCase):
    def legacy(self):
        c = migrate(copy.deepcopy(collection.DEFAULTS))
        c.pop("history_frequency_version")
        c["frequency_groups"] = c["frequency_groups"][:6]
        c["task_modes"] = {}
        return c

    def test_history_split_preserves_today_orders_and_both_concurrency_layers(self):
        c = self.legacy()
        c.update(max_concurrent=17, shop_concurrent=4, shop_limits={"9": 2})
        c["platform_modes"] = {
            "pdd": {"transactions": "realtime", "ad_reports": "realtime", "goodsdata": "custom2", "orders": "custom"}
        }
        c["task_modes"] = {"pdd": {"ad_reports@base": "custom", "orders@yesterday": "realtime"}}
        before = copy.deepcopy(c)
        result = migrate(c)
        self.assertEqual(c, before)
        self.assertEqual(result["frequency_groups"][:6], before["frequency_groups"])
        for key in ("max_concurrent", "shop_concurrent", "shop_limits", "platform_modes"):
            self.assertEqual(result[key], before[key])
        self.assertEqual(group("ad_reports", {"trade_range": "realtime"}, result, "pdd")["id"], "custom")
        self.assertEqual(group("transactions", {"trade_range": "realtime"}, result, "pdd")["id"], "realtime")
        self.assertEqual(result["task_modes"]["pdd"]["orders@yesterday"], "realtime")
        self.assertEqual(group("ad_reports", {"trade_range": "yesterday"}, result, "pdd")["seconds"], 900)
        for span in ("week", "month"):
            g = group("ad_reports", {"trade_range": span}, result, "pdd")
            self.assertEqual(g["seconds"], 3600)
            self.assertEqual(g["time"], "")
        self.assertEqual(migrate(result), result)

    def test_explicit_history_choices_and_pauses_are_preserved(self):
        c = self.legacy()
        c["platform_modes"] = {"pdd": {"ad_products": "off"}}
        c["task_modes"] = {"pdd": {"ad_reports@yesterday": "realtime", "transactions@month": "off"}}
        result = migrate(c)
        self.assertEqual(result["task_modes"]["pdd"]["ad_reports@yesterday"], "realtime")
        self.assertEqual(result["task_modes"]["pdd"]["transactions@month"], "off")
        self.assertEqual(result["task_modes"]["pdd"]["ad_products@week"], "off")
        self.assertNotIn("jd", result["task_modes"])

    def test_recommendation_can_move_explicit_history_without_rewriting_groups(self):
        c = migrate(self.legacy())
        old = copy.deepcopy(c["frequency_groups"])
        c["task_modes"]["pdd"].update(
            {"ad_reports@yesterday": "realtime", "ad_reports@base": "realtime", "goodsdata@month": "off"}
        )
        ordinary = recommend(c)
        self.assertNotIn(
            "ad_reports@yesterday", [a["kind"] for a in ordinary["module_assignments"] if a["platform"] == "pdd"]
        )
        plan = recommend(c, include_explicit=True)
        assignments = {(a["platform"], a["kind"]): a["target"] for a in plan["module_assignments"]}
        self.assertEqual(assignments[("pdd", "ad_reports@yesterday")], "history_yesterday")
        self.assertNotIn(("pdd", "ad_reports@base"), assignments)
        self.assertNotIn(("pdd", "goodsdata@month"), assignments)
        self.assertEqual(c["frequency_groups"], old)

    def test_conflicting_group_ids_and_names_do_not_change_existing_values(self):
        c = self.legacy()
        existing = {"id": "history_yesterday", "name": "昨日报表", "seconds": 37, "time": "", "timeout": 22}
        c["frequency_groups"].append(existing)
        result = migrate(c)
        self.assertEqual(next(g for g in result["frequency_groups"] if g["id"] == "history_yesterday"), existing)
        self.assertEqual(result["task_modes"]["pdd"]["ad_reports@yesterday"], "history_yesterday_2")
        self.assertEqual(len({g["name"] for g in result["frequency_groups"]}), len(result["frequency_groups"]))

    def test_slower_inherited_and_explicit_settings_are_never_accelerated(self):
        c = self.legacy()
        c["platform_modes"] = {"pdd": {"ad_reports": "daily", "goodsdata": "custom3"}}
        c["task_modes"] = {"pdd": {"transactions@yesterday": "daily", "ad_products@month": "off"}}
        for g in c["frequency_groups"]:
            if g["id"] == "custom3":
                g["seconds"] = 7200
        result = migrate(c)
        self.assertEqual(result["task_modes"]["pdd"]["ad_reports@yesterday"], "daily")
        self.assertEqual(result["task_modes"]["pdd"]["goodsdata@month"], "custom3")
        plan = recommend(result, include_explicit=True)
        changes = {(a["platform"], a["kind"]) for a in plan["module_assignments"]}
        self.assertNotIn(("pdd", "ad_reports@yesterday"), changes)
        self.assertNotIn(("pdd", "goodsdata@month"), changes)
        self.assertNotIn(("pdd", "transactions@yesterday"), changes)
        self.assertNotIn(("pdd", "ad_products@month"), changes)

    def test_user_drag_and_group_delete_survive_settings_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            c = collection.settings(store)
            c = collection.save_settings(
                store,
                {
                    "module_assignments": [
                        {"platform": "pdd", "kind": "transactions@reports", "tab": "task", "target": "custom2"}
                    ]
                },
            )
            c = collection.save_settings(
                store,
                {
                    "settings_patch": {
                        "group_updates": {"history_rolling": None},
                        "group_moves": {"history_rolling": "daily"},
                    }
                },
            )
            reloaded = collection.settings(store)
            self.assertEqual(reloaded, c)
            self.assertEqual(reloaded["task_modes"]["pdd"]["ad_reports@yesterday"], "custom2")
            self.assertEqual(reloaded["task_modes"]["pdd"]["ad_reports@month"], "custom2")
            self.assertNotIn("history_rolling", [g["id"] for g in reloaded["frequency_groups"]])

    def test_history_classification_excludes_live_order_and_tax_work(self):
        self.assertFalse(historical("ad_reports", {"trade_range": "quarter"}))
        self.assertFalse(historical("ad_reports", {"trade_range": "realtime"}))
        self.assertFalse(historical("orders", {"trade_range": "yesterday", "_order_lane": "recent"}))
        self.assertFalse(historical("tax", {"period": "2026-Q3"}))
