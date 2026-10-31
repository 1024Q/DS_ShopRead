import asyncio, copy, tempfile, time, unittest
from types import SimpleNamespace
from backend import collection
from backend.store import Store
from backend.frequency_policy import migrate, group, task_id, next_run
from backend.collection_queue import select_jobs, waiting_reason
from backend.center.configuration import matches_patch
from backend.business import BusinessService
from backend.collector_task_list import listing


class FrequencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.config = collection.settings(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_migration_once_preserves_values(self):
        self.assertEqual(
            [g["id"] for g in self.config["frequency_groups"][:6]],
            ["realtime", "custom", "daily", "custom2", "custom3", "custom4"],
        )
        self.assertEqual(len(self.config["frequency_groups"]), 8)
        self.assertEqual(self.config["max_concurrent"], 20)
        self.assertEqual(self.config["shop_concurrent"], 3)
        self.assertEqual(migrate(self.config), self.config)
        old = {**collection.DEFAULTS, "max_concurrent": 16, "custom_seconds": 420, "daily_time": "06:30"}
        new = migrate(old)
        self.assertEqual(new["max_concurrent"], 16)
        self.assertEqual(new["frequency_groups"][1]["seconds"], 420)

    def test_task_assignment_independent_and_patch_confirmed(self):
        patch = {
            "module_assignments": [{"platform": "pdd", "kind": "orders@unfinished", "tab": "task", "target": "custom2"}]
        }
        c = collection.save_settings(self.store, patch)
        self.assertEqual(group("orders", {"status": "待付款"}, c, "pdd")["id"], "custom2")
        self.assertEqual(group("orders", {"status": "待发货"}, c, "pdd")["id"], "custom2")
        self.assertNotEqual(group("orders", {"status": "退款成功"}, c, "pdd")["id"], "custom2")
        self.assertTrue(matches_patch("system/settings", patch, c))

    def test_partial_group_edit_preserves_other_fields(self):
        patch = {"settings_patch": {"group_updates": {"custom": {"timeout": 123}}}}
        c = collection.save_settings(self.store, patch)
        g = next(g for g in c["frequency_groups"] if g["id"] == "custom")
        self.assertEqual(g["seconds"], 300)
        self.assertEqual(g["timeout"], 123)
        self.assertTrue(matches_patch("system/settings", patch, c))

    def test_delete_moves_tasks_atomically(self):
        c = collection.save_settings(
            self.store,
            {
                "module_assignments": [
                    {"platform": "pdd", "kind": "orders@unfinished", "tab": "task", "target": "custom2"}
                ]
            },
        )
        with self.assertRaises(ValueError):
            collection.save_settings(self.store, {"settings_patch": {"group_updates": {"custom2": None}}})
        self.assertEqual(collection.settings(self.store), c)
        c = collection.save_settings(
            self.store, {"settings_patch": {"group_updates": {"custom2": None}, "group_moves": {"custom2": "custom3"}}}
        )
        self.assertEqual(group("orders", {"status": "待付款"}, c, "pdd")["id"], "custom3")

    def test_group_interval_drives_due(self):
        c = collection.save_settings(self.store, {"settings_patch": {"group_updates": {"realtime": {"seconds": 13}}}})
        c["task_modes"] = {"pdd": {"orders@unpaid": "realtime"}}
        self.assertFalse(collection.due("orders", {"status": "待付款"}, 100, c, 112))
        self.assertTrue(collection.due("orders", {"status": "待付款"}, 100, c, 113))

    def test_only_two_caps_and_parent_exclusion(self):
        c = {
            **self.config,
            "max_concurrent": 5,
            "shop_concurrent": 3,
            "shop_limits": {"1": 1},
            "retry_concurrent": 1,
            "borrow_idle": False,
            "max_active_shops": 3,
        }
        jobs = {
            (1, "a"): {"kind": "orders", "phase": "running", "started_at": 10, "scope": {}},
            (1, "b"): {"kind": "products", "phase": "queued", "queued_at": 1, "scope": {}},
            (2, "a"): {
                "kind": "orders",
                "phase": "queued",
                "queued_at": 1,
                "scope": {"status": "待付款"},
                "failures": 2,
            },
            (2, "b"): {
                "kind": "orders",
                "phase": "queued",
                "queued_at": 2,
                "scope": {"status": "待发货"},
                "failures": 2,
            },
        }
        jobs.update(
            {
                (3, k): {"kind": k, "phase": "queued", "queued_at": 3, "scope": {}, "failures": 2}
                for k in ["overview", "products", "reviews"]
            }
        )
        winners = select_jobs(jobs, c, now=1000)
        self.assertEqual(len(winners), 4)
        self.assertNotIn((1, "b"), winners)
        self.assertEqual(sum(k[0] == 2 for k in winners), 1)
        self.assertIn("单店铺", waiting_reason((1, "b"), jobs[(1, "b")], jobs, c))

    def test_categories_cover_fixed_status_queries(self):
        self.assertEqual(task_id("aftersales", {"status": "待商家处理"}), "aftersales@merchant_pending")
        self.assertEqual(task_id("tickets", {"status": "待处理"}), "tickets@pending")
        self.assertEqual(task_id("ad_reports", {"trade_range": "week"}), "ad_reports@week")

    def test_task_list_reports_actual_timing(self):
        now = time.time()
        m = {
            "kind": "orders",
            "phase": "running",
            "scope": {"status": "待付款"},
            "queued_at": now - 20,
            "started_at": now - 5,
            "timeout_seconds": 30,
            "stage": "读取第2页",
        }
        service = SimpleNamespace(
            store=SimpleNamespace(list=lambda: [{"id": 1, "platform": "pdd", "name": "测试店"}]),
            progress={(1, "key"): m},
            queue_config=self.config,
        )
        row = listing(service, "running", 1, {}, {}, {})["rows"][0]
        self.assertIn("未完结订单回查", row["module"])
        self.assertIn("待付款", row["current_step"])
        self.assertEqual(row["elapsed"], "5 秒")
        self.assertEqual(row["remaining"], "25 秒")
        self.assertNotEqual(row["frequency"], "—")


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_releases_lock_and_immediate_refill(self):
        service = BusinessService(None, lambda _: None)
        service.queue_config = migrate(copy.deepcopy(collection.DEFAULTS))
        service.queue_config["max_concurrent"] = 1
        for g in service.queue_config["frequency_groups"]:
            g["timeout"] = 0.05
        item = SimpleNamespace(lock=asyncio.Lock(), shop={"id": 1})
        entered = asyncio.Event()

        async def first():
            with self.assertRaises(TimeoutError):
                async with service.slot((1, "orders:a"), item, "orders"):
                    entered.set()
                    await asyncio.sleep(2)

        async def second():
            await entered.wait()
            async with service.slot((1, "products:b"), item, "products"):
                return True

        a, b = await asyncio.wait_for(asyncio.gather(first(), second()), 1)
        self.assertTrue(b)
        self.assertFalse(item.lock.locked())
        self.assertFalse(service.progress)

    async def test_running_timeout_is_captured(self):
        service = BusinessService(None, lambda _: None)
        service.queue_config = migrate(copy.deepcopy(collection.DEFAULTS))
        item = SimpleNamespace(lock=asyncio.Lock(), shop={"id": 1})
        async with service.slot((1, "orders:a"), item, "orders") as meta:
            old = meta["timeout_seconds"]
            service.queue_config["frequency_groups"][1]["timeout"] = 1
            self.assertEqual(meta["timeout_seconds"], old)
