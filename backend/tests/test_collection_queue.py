import asyncio, tempfile, unittest
from types import SimpleNamespace
from backend import collection
from backend.store import Store
from backend.business import BusinessService
from backend.collection_queue import select_jobs, queue_for, queue_status
from backend.tests.collection_fixture import policy, assign


def job(kind, phase="queued", at=1, **values):
    return {"kind": kind, "phase": phase, "queued_at": at, "scope": {}, **values}


class AdmissionTests(unittest.TestCase):
    def test_only_node_shop_and_active_shop_caps_apply(self):
        kinds = ["orders", "products", "reviews"]
        tasks = {(sid, kind): job(kind) for sid in range(1, 5) for kind in kinds}
        for total, per_shop, shops in [(4, 2, 2), (20, 3, 2), (20, 1, 4)]:
            c = policy(max_concurrent=total, shop_concurrent=per_shop, max_active_shops=shops)
            chosen = select_jobs(tasks, c, now=1000)
            self.assertEqual(len(chosen), min(total, per_shop * shops))
            self.assertLessEqual(len({k[0] for k in chosen}), shops)
            self.assertTrue(all(sum(k[0] == sid for k in chosen) <= per_shop for sid in range(1, 5)))

    def test_same_module_is_exclusive_across_frequency_groups(self):
        tasks = {
            (1, "live"): job("orders", phase="running", queue="realtime"),
            (1, "history"): job("orders"),
            (1, "products"): job("products"),
        }
        self.assertEqual(select_jobs(tasks, policy(max_concurrent=3), now=1000), [(1, "products")])

    def test_due_age_wins_without_hidden_priority_or_reserved_quota(self):
        c = policy(max_concurrent=1, borrow_idle=False, retry_concurrent=0, realtime_concurrent=0)
        tasks = {
            (1, "old"): job("products", at=1, failures=2),
            (2, "new"): job("overview", at=100, scope={"_attention": True}, priority="high"),
        }
        self.assertEqual(select_jobs(tasks, c, now=1000), [(1, "old")])
        tasks[(1, "old")]["retry_at"] = 2000
        self.assertEqual(select_jobs(tasks, c, now=1000), [(2, "new")])

    def test_lower_cap_preserves_running_and_refills_after_completion(self):
        tasks = {
            (1, "a"): job("orders", phase="running", queue="realtime"),
            (2, "b"): job("products", phase="running"),
            (3, "c"): job("reviews"),
        }
        c = policy(max_concurrent=1, max_active_shops=3)
        self.assertEqual(select_jobs(tasks, c), [])
        self.assertEqual(queue_for(tasks[(1, "a")], c), "realtime")
        del tasks[(1, "a")]
        del tasks[(2, "b")]
        self.assertEqual(select_jobs(tasks, c), [(3, "c")])


class SettingsTests(unittest.TestCase):
    def test_partial_updates_preserve_frequency_sleep_and_other_shops(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            before = collection.settings(store)
            after = collection.save_settings(store, {"settings_patch": {"max_concurrent": 37, "shop_limits": {"1": 5}}})
            for key in ("frequency_groups", "task_modes", "browser_idle_seconds", "max_active_shops"):
                self.assertEqual(after[key], before[key])
            self.assertEqual(collection.settings(Store(folder)), after)
            self.assertEqual(after["max_concurrent"], 37)

    def test_assignment_changes_cadence_without_queue_quotas(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            c = assign(store, {"orders@unfinished": "custom2"})
            self.assertEqual(queue_for(job("orders", scope={"status": "待发货"}), c), "custom2")
            before = collection.settings(store)
            for data in (
                {"platform_queues": {"pdd": {"orders": "focus"}}},
                {"platform_priorities": {}},
                {"realtime_concurrent": 3},
            ):
                with self.assertRaises(ValueError):
                    collection.save_settings(store, data)
                self.assertEqual(collection.settings(store), before)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_setting_change_wakes_queue_and_cancellation_releases_capacity(self):
        service = BusinessService(None, None)
        service.configure_queue(
            {
                **policy(max_active_shops=4),
                "max_concurrent": 2,
                "realtime_concurrent": 1,
                "daily_concurrent": 1,
                "borrow_idle": False,
            }
        )
        entered = []
        gate = asyncio.Event()
        items = {i: SimpleNamespace(lock=asyncio.Lock()) for i in range(1, 5)}

        async def run(sid, kind):
            service.progress[(sid, kind)] = {
                "kind": kind,
                "scope": {"_attention": True} if sid < 4 else {},
                "queued_at": 0,
                "phase": "queued",
            }
            async with service.slot((sid, kind), items[sid], kind):
                entered.append(sid)
                await gate.wait()

        tasks = [asyncio.create_task(run(i, "overview" if i < 4 else "products")) for i in range(1, 5)]
        try:
            await asyncio.sleep(0.02)
            self.assertEqual(set(entered), {1, 2})
            service.configure_queue(policy(max_active_shops=4))
            await asyncio.sleep(0.02)
            self.assertEqual(set(entered), {1, 2, 3, 4})
            tasks[0].cancel()
            await asyncio.gather(tasks[0], return_exceptions=True)
            self.assertNotIn(1, service.running_shops)
            gate.set()
            await asyncio.wait_for(asyncio.gather(*tasks[1:]), 1)
            self.assertFalse(service.running_shops)
            self.assertFalse(service.progress)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class ApiTests(unittest.TestCase):
    def test_concurrency_auth_validation_and_live_update(self):
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from backend import main

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            service = BusinessService(store, None)
            with (
                patch.object(main, "store", store),
                patch.object(main, "business", service),
                TestClient(main.app) as client,
            ):
                headers = {"Authorization": "Bearer " + main.TOKEN}
                body = {"max_concurrent": 31, "shop_concurrent": 4, "max_active_shops": 2}
                self.assertEqual(client.put("/api/system/concurrency", json=body).status_code, 401)
                for invalid in (
                    {"max_concurrent": 0},
                    {"shop_concurrent": True},
                    {"realtime_concurrent": 3},
                    {"daily_time": "22:00"},
                ):
                    self.assertEqual(
                        client.put("/api/system/concurrency", headers=headers, json=invalid).status_code, 400
                    )
                before = collection.settings(store)
                result = client.put("/api/system/concurrency", headers=headers, json=body)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(service.queue_config["max_concurrent"], 31)
                saved = collection.settings(Store(folder))
                self.assertEqual(saved["shop_concurrent"], 4)
                self.assertEqual(saved["frequency_groups"], before["frequency_groups"])
                self.assertEqual(saved["browser_idle_seconds"], before["browser_idle_seconds"])
