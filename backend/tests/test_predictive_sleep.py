import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend.browser_sleep import maintain
from backend.tests.collection_fixture import policy


class PredictiveSleepTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        item = SimpleNamespace(
            retired=False,
            wanted_running=True,
            sleeping=False,
            state="logged_in",
            lock=asyncio.Lock(),
            shop={"id": 1, "platform": "pdd"},
            last_collection_activity=100,
            collection_wake_seconds=20,
            stop=AsyncMock(),
            recovery_status="",
        )
        service = SimpleNamespace(
            queue_config=policy(browser_idle_seconds=30, max_active_shops=2, home_paused=False),
            progress={},
            deferred={(1, "products"): dict(kind="products", scope={}, stage="等待下次执行", next_run_at=1020)},
            round_candidates=set(),
        )
        return item, service

    async def check(self, item, service, mono=131, wall=1000):
        with (
            patch("backend.browser_sleep.time.monotonic", return_value=mono),
            patch("backend.browser_sleep.time.time", return_value=wall),
        ):
            await maintain(item, service)

    async def test_nearby_enabled_task_holds_browser_without_changing_config(self):
        item, service = self.fixture()
        before = copy.deepcopy(service.queue_config)
        await self.check(item, service)
        item.stop.assert_not_awaited()
        self.assertIn("暂缓休眠", item.recovery_status)
        self.assertEqual(service.queue_config, before)
        self.assertEqual(item.last_collection_activity, 100)

    async def test_deferral_deadline_does_not_slide_with_new_due_times(self):
        item, service = self.fixture()
        await self.check(item, service)
        service.deferred[(1, "products")]["next_run_at"] = 1040
        await self.check(item, service, mono=151, wall=1020)
        item.stop.assert_not_awaited()
        # A further scheduling update cannot extend the original idle boundary.
        service.deferred[(1, "products")]["next_run_at"] = 1070
        await self.check(item, service, mono=181, wall=1050)
        item.stop.assert_awaited_once_with(sleep=True)

    async def test_paused_disabled_and_missing_demand_never_hold(self):
        for change in ("paused", "disabled", "missing", "off"):
            item, service = self.fixture()
            if change == "paused":
                service.queue_config["home_paused"] = True
            elif change == "disabled":
                service.queue_config["enabled"] = False
            elif change == "missing":
                service.deferred = {}
            else:
                from backend.task_catalog import grouped_id

                service.queue_config.setdefault("task_modes", {}).setdefault("pdd", {})[grouped_id("products@base")] = (
                    "off"
                )
            await self.check(item, service)
            item.stop.assert_awaited_once_with(sleep=True)

    async def test_due_peers_or_full_active_shop_slots_release_browser(self):
        for condition in ("queued", "due", "full", "warm"):
            item, service = self.fixture()
            if condition == "queued":
                service.progress[(2, "products")] = {"phase": "queued"}
            elif condition == "due":
                service.round_candidates = {(2, "products")}
            elif condition == "full":
                service.progress = {(2, "x"): {"phase": "running"}, (3, "y"): {"phase": "running"}}
            else:
                service.browser_sleep_holds = {2: 170, 3: 170}
            await self.check(item, service)
            item.stop.assert_awaited_once_with(sleep=True)


if __name__ == "__main__":
    unittest.main()
