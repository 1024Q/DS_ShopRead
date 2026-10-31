import copy, tempfile, unittest
from backend import collection
from backend.store import Store
from backend.frequency_policy import catalog, group, save
from backend.task_catalog import REMOVED, GROUPS, HOME, title, LEGACY_GROUPS


class TaskCatalogTests(unittest.TestCase):
    def test_catalog_filters_and_labels(self):
        entries = {t["id"]: t for t in catalog()}
        self.assertFalse(REMOVED & entries.keys())
        self.assertEqual(set(entries["overview@home"]["members"]), HOME - {"products@base"})
        self.assertEqual(entries["products@base"]["label"], "首页-商品列表-常规任务")
        self.assertFalse(set(LEGACY_GROUPS) & entries.keys())
        for identifier, (_, members) in GROUPS.items():
            self.assertEqual(entries[identifier]["members"], list(members))
            self.assertFalse(set(members) & entries.keys())
        members = [m for _, items in GROUPS.values() for m in items]
        self.assertEqual(len(members), len(set(members)))

    def test_save_group_updates_members_without_changing_limits(self):
        with tempfile.TemporaryDirectory() as path:
            store = Store(path)
            config = collection.settings(store)
            before = copy.deepcopy(config)
            for identifier, (_, members) in GROUPS.items():
                result = save(
                    store,
                    {"module_assignments": [dict(platform="pdd", kind=identifier, tab="task", target="custom2")]},
                    config,
                )
                for member in members:
                    self.assertEqual(result["task_modes"]["pdd"][member], "custom2")
                for field in ("frequency_groups", "max_concurrent", "shop_concurrent", "shop_limits"):
                    self.assertEqual(result[field], before[field])
                for member in members:
                    kind, cat = member.split("@")
                    scope = (
                        {"status": "待商家处理"}
                        if cat == "merchant_pending"
                        else {"trade_range": cat}
                        if cat in ("yesterday", "week", "month")
                        else {}
                    )
                    if cat == "attention":
                        scope = {"_attention": True}
                    if cat == "expiring":
                        scope = {"status": "待处理即将逾期"}
                    if kind == "tickets" and cat == "pending":
                        scope = {"status": "待处理"}
                    if kind == "orders":
                        from datetime import datetime, timedelta
                        from backend.business_reader import BEIJING

                        if cat in ("today", "yesterday"):
                            scope = {
                                "_order_lane": "recent",
                                "date_from": (
                                    datetime.now(BEIJING).date() - timedelta(days=cat == "yesterday")
                                ).isoformat(),
                            }
                        else:
                            scope = {
                                "status": {
                                    "unpaid": "待付款",
                                    "unshipped": "待发货",
                                    "unreceived": "待收货",
                                    "refund": "退款成功",
                                    "shipping_due": "即将逾期发货",
                                }[cat]
                            }
                    self.assertEqual(group(kind, scope, result, "pdd")["id"], "custom2")
                config = result
            self.assertEqual(group("products", {}, result, "pdd"), group("products", {}, before, "pdd"))

    def test_legacy_group_preserved_and_new_save_survives_reload(self):
        with tempfile.TemporaryDirectory() as path:
            store = Store(path)
            config = collection.settings(store)
            config.pop("task_layout")  # Simulate a policy saved before leaf-frequency migration.
            config["task_modes"] = {"pdd": {"ad_reports@history": "daily", "ad_reports@week": "custom"}}
            from backend.frequency_policy import migrate

            config = migrate(config)
            self.assertEqual(group("ad_reports", {"trade_range": "week"}, config, "pdd")["id"], "daily")
            result = save(
                store,
                {
                    "module_assignments": [
                        dict(platform="pdd", kind="transactions@reports", tab="task", target="custom2")
                    ]
                },
                config,
            )
            self.assertEqual(collection.settings(store), result)
            for legacy in LEGACY_GROUPS:
                if legacy != "aftersales@service":
                    self.assertEqual(result["task_modes"]["pdd"][legacy], "custom2")

    def test_two_new_groups_preserve_old_settings_and_isolate_home(self):
        from backend.frequency_policy import migrate

        with tempfile.TemporaryDirectory() as path:
            store = Store(path)
            config = collection.settings(store)
            config.pop("task_layout")  # These legacy parent values must migrate only once.
            config["task_modes"] = {
                "pdd": {
                    "aftersales@service": "daily",
                    "aftersales@base": "custom",
                    "tickets@pending": "custom3",
                    "ad_products@base": "realtime",
                    "goodsdata@base": "custom",
                }
            }
            config = migrate(config)
            self.assertEqual(group("aftersales", {}, config, "pdd")["id"], "daily")
            self.assertEqual(group("tickets", {"status": "待处理"}, config, "pdd")["id"], "custom3")
            self.assertEqual(group("ad_products", {}, config, "pdd")["id"], "realtime")
            self.assertEqual(group("goodsdata", {}, config, "pdd")["id"], "custom")
            home_before = [group(k, {"_attention": True}, config, "pdd") for k in ("aftersales", "tickets")]
            result = save(
                store,
                {
                    "module_assignments": [
                        dict(platform="pdd", kind=k, tab="task", target="custom2")
                        for k in ("goodsdata@live", "aftersales@work")
                    ]
                },
                config,
            )
            self.assertEqual(collection.settings(store), result)
            self.assertEqual(result["task_modes"]["pdd"]["aftersales@service"], "custom2")
            self.assertEqual(
                home_before, [group(k, {"_attention": True}, result, "pdd") for k in ("aftersales", "tickets")]
            )
            self.assertEqual(len(catalog()), 11)
            self.assertEqual(title("tickets", {"status": "待处理"}), "售后与工单待办 / 工单列表-待处理工单")

    def test_removed_reports_never_retry_even_if_configured(self):
        from backend.task_catalog import REPORT_KINDS
        from backend.task_lifecycle import retained
        from backend.business import BusinessService

        for kind in REPORT_KINDS:
            scope = {"trade_range": "quarter"}
            self.assertFalse(retained({"kind": kind}, scope, fixed=True))
            self.assertIsNone(
                group(
                    kind,
                    scope,
                    {
                        "frequency_version": 1,
                        "frequency_groups": [{"id": "custom"}],
                        "task_modes": {"pdd": {kind + "@quarter": "custom"}},
                    },
                    "pdd",
                )
            )
            cached = {"rows": [{"id": "saved"}]}
            self.assertIs(
                BusinessService.admit(None, {}, None, kind, (scope, "key", cached, {}, {}), True, True, True), cached
            )

    def test_invalid_catalog_cannot_be_saved(self):
        with tempfile.TemporaryDirectory() as path:
            store = Store(path)
            config = collection.settings(store)
            for identifier in REMOVED:
                with self.assertRaises(ValueError):
                    save(
                        store,
                        {"module_assignments": [dict(platform="pdd", kind=identifier, tab="task", target="custom")]},
                        config,
                    )

    def test_runtime_names(self):
        self.assertEqual(title("orders", {"status": "退款成功"}), "首页数据综合采集 / 订单查询-退款成功订单")
        self.assertTrue(title("quality", {}).startswith("商品诊断与权益 / "))
