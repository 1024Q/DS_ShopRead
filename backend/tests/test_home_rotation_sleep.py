import asyncio, copy, json, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend import collection, collection_checkpoint as cp
from backend.business import BusinessService, options_for
from backend.browser import Session
from backend.browser_recovery import remember, check
from backend.browser_sleep import maintain, wake, manual_action
from backend.collection_queue import select_jobs
from backend.frequency_policy import migrate
from backend.home_collection_control import change
from backend.shop_lock import ShopLock
from backend.shop_rotation import ShopRotation
from backend.store import Store


class RotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_shops_and_immediate_replacement(self):
        service = BusinessService(None, None)
        service.queue_config = migrate(copy.deepcopy(collection.DEFAULTS))
        service.queue_config.update(max_concurrent=20, shop_concurrent=3)
        entered = [asyncio.Event() for _ in range(4)]
        release = [asyncio.Event() for _ in range(4)]
        jobs = []

        async def run(n):
            item = SimpleNamespace(lock=ShopLock(), shop={"id": n + 1})
            async with service.slot((n + 1, "overview"), item, "overview"):
                entered[n].set()
                await release[n].wait()

        try:
            jobs = [asyncio.create_task(run(n)) for n in range(4)]
            await asyncio.wait_for(asyncio.gather(entered[0].wait(), entered[1].wait()), 1)
            self.assertFalse(entered[2].is_set())
            self.assertFalse(entered[3].is_set())
            release[0].set()
            await asyncio.wait_for(entered[2].wait(), 1)
            self.assertEqual(len({k[0] for k, m in service.progress.items() if m["phase"] == "running"}), 2)
        finally:
            for j in jobs:
                j.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await service.close()

    def test_round_does_not_append_new_demands(self):
        rotation = ShopRotation()
        progress = {(1, "a"): {"phase": "queued"}, (1, "b"): {"phase": "queued"}, (2, "a"): {"phase": "queued"}}
        rotation.reserve((1, "a"), progress)
        progress[1, "late"] = {"phase": "queued"}
        rotation.refresh(progress)
        self.assertTrue(progress[1, "late"]["shop_round_wait"])
        rotation.complete((1, "a"))
        rotation.complete((1, "b"))
        progress.pop((1, "a"))
        progress.pop((1, "b"))
        rotation.refresh(progress)
        self.assertFalse(progress[1, "late"]["shop_round_wait"])
        self.assertGreater(progress[1, "late"]["shop_turn_order"], progress[2, "a"]["shop_turn_order"])

    def test_grants_count_as_occupied_shop_slots(self):
        config = migrate(copy.deepcopy(collection.DEFAULTS))
        config["_rotation_shops"] = [1, 2]
        jobs = {(3, "a"): {"kind": "overview", "phase": "queued", "scope": {}, "queued_at": 1}}
        self.assertEqual(select_jobs(jobs, config), [])


class PauseTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_wake_releases_grant_and_persists_retry_backoff(self):
        from backend.browser_sleep import remember as remember_sleep

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("fixture", "pw")
            item = Session(store.list()[0], store)
            remember(item, True)
            remember_sleep(item, True)
            item.state = "sleeping"
            item.start = AsyncMock(side_effect=TimeoutError())
            service = BusinessService(store, lambda _: item)
            scope = options_for("products")
            key = service.key("products", scope, item.shop)
            try:
                service.request_sync("products", scope)
                await asyncio.gather(*service.due_plans.preparations.values())
                state = collection.state(store, sid, key)
                self.assertEqual(state["failures"], 0)
                self.assertGreater(service.due_plans.browser_failures[sid]["until"], time.time())
                self.assertEqual(state["last_success"], 0)
                self.assertFalse(service.jobs)
                self.assertFalse(service.slot_dispatcher.grants)
                self.assertTrue(item.sleeping)
            finally:
                await service.close()

    async def test_node_parameters_adjust_without_changing_frequencies(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            before = collection.settings(store)
            self.assertEqual(before["max_active_shops"], 2)
            self.assertEqual(before["browser_idle_seconds"], 30)
            after = collection.save_settings(
                store, {"settings_patch": {"max_active_shops": 4, "browser_idle_seconds": 0}}
            )
            self.assertEqual(after["max_active_shops"], 4)
            self.assertEqual(after["browser_idle_seconds"], 0)
            self.assertEqual(after["frequency_groups"], before["frequency_groups"])
            with self.assertRaises(ValueError):
                collection.save_settings(store, {"settings_patch": {"max_active_shops": 0}})

    async def test_pause_cancels_read_retains_checkpoint_and_resume_keeps_frequency(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("fixture", "pw")
            item = Session(store.list()[0], store)
            item.state = "logged_in"
            item.window = AsyncMock()
            service = BusinessService(store, lambda _: item)
            scope = options_for("products")
            key = service.key("products", scope, item.shop)
            reading = asyncio.Event()

            async def read_page(n):
                if n == 2:
                    reading.set()
                    await asyncio.sleep(20)
                return {"rows": [{"id": str(n)}], "ids": [str(n)], "total": 2}

            async def read(*_):
                return await cp.paginate("part", read_page, lambda *_: None)

            try:
                with patch("backend.business.collect", new=read):
                    service.request_sync("products", scope, force=True)
                    await asyncio.wait_for(reading.wait(), 2)
                    frequencies = copy.deepcopy(service.queue_config["frequency_groups"])
                    result = await change(service, True)
                    self.assertTrue(result["confirmed"])
                    self.assertFalse(service.jobs)
                    from backend.master_tasks import project

                    self.assertEqual(project(service), [])
                    self.assertEqual(collection.mode("products", scope, service.queue_config), "off")
                    self.assertEqual(collection.state(store, sid, key)["failures"], 0)
                    with cp.context(store, sid, key, {}) as saved:
                        self.assertEqual(next(iter(saved.data["parts"].values()))["ids"], ["1"])
                    await change(service, False)
                    self.assertEqual(frequencies, service.queue_config["frequency_groups"])
                    self.assertNotEqual(collection.mode("products", scope, service.queue_config), "off")
                    self.assertEqual(collection.mode("goodsdata", {}, service.queue_config), "off")
            finally:
                await service.close()


class SleepTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_browser_actions_wake_sleeping_shop(self):
        for action in ("orders", "show", "home", "retry", "maximize"):
            item = SimpleNamespace(sleeping=True, start=AsyncMock())
            self.assertFalse(await manual_action(item, action))
            item.start.assert_awaited_once_with()

    async def test_hide_sleeping_shop_is_noop_and_active_shop_does_not_restart(self):
        item = SimpleNamespace(sleeping=True, start=AsyncMock())
        self.assertTrue(await manual_action(item, "hide"))
        item.start.assert_not_awaited()
        item.sleeping = False
        self.assertFalse(await manual_action(item, "show"))
        item.start.assert_not_awaited()

    async def test_sleep_preserves_enabled_intent_across_restart_and_no_recovery(self):
        import httpx

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("fixture", "pw")
            item = Session(store.list()[0], store)
            remember(item, True)
            item.state = "logged_in"
            item.last_collection_activity = time.monotonic() - 31
            service = SimpleNamespace(queue_config={"browser_idle_seconds": 30}, progress={})
            client = AsyncMock()
            client.get.side_effect = httpx.ConnectError("closed")
            context = AsyncMock()
            context.__aenter__.return_value = client
            with patch("backend.browser.httpx.AsyncClient", return_value=context):
                await maintain(item, service)
            self.assertTrue(item.wanted_running)
            self.assertTrue(item.sleeping)
            self.assertEqual(item.state, "sleeping")
            restarted = Session(store.list()[0], store)
            self.assertTrue(restarted.sleeping)
            self.assertTrue(restarted.wanted_running)
            restarted.start = AsyncMock()
            restarted.refresh = AsyncMock()
            await check(restarted)
            restarted.start.assert_not_awaited()
            restarted.refresh.assert_not_awaited()

    async def test_manual_control_and_verification_never_closed(self):
        for values in (
            {"remote_control_active": True},
            {"control_until": time.time() + 60},
            {"state": "needs_verification"},
        ):
            item = SimpleNamespace(
                retired=False,
                wanted_running=True,
                sleeping=False,
                state="logged_in",
                visible=False,
                lock=ShopLock(),
                shop={"id": 1},
                last_collection_activity=time.monotonic() - 100,
                stop=AsyncMock(),
                **{},
            )
            for k, v in values.items():
                setattr(item, k, v)
            await maintain(item, SimpleNamespace(queue_config={"browser_idle_seconds": 30}, progress={}))
            item.stop.assert_not_awaited()

    async def test_all_idle_shops_sleep_even_visible_or_round_reserved(self):
        service = SimpleNamespace(
            queue_config={"browser_idle_seconds": 30},
            progress={},
            shop_rotation=SimpleNamespace(rounds={1: {(1, "queued")}}),
        )
        for sid in (1, 2, 3):
            item = SimpleNamespace(
                retired=False,
                wanted_running=True,
                sleeping=False,
                state="logged_in",
                visible=True,
                lock=ShopLock(),
                shop={"id": sid},
                last_collection_activity=time.monotonic() - 31,
                stop=AsyncMock(),
            )
            await maintain(item, service)
            item.stop.assert_awaited_once_with(sleep=True)

    async def test_running_and_granted_tasks_keep_browser_awake(self):
        for progress, grants in (({(1, "task"): {"phase": "running"}}, set()), ({}, {(1, "task")})):
            service = SimpleNamespace(
                queue_config={"browser_idle_seconds": 30},
                progress=progress,
                slot_dispatcher=SimpleNamespace(grants=grants),
            )
            item = SimpleNamespace(
                retired=False,
                wanted_running=True,
                sleeping=False,
                state="logged_in",
                visible=True,
                lock=ShopLock(),
                shop={"id": 1},
                last_collection_activity=time.monotonic() - 31,
                stop=AsyncMock(),
            )
            await maintain(item, service)
            item.stop.assert_not_awaited()

    async def test_wakes_coalesce_before_collection(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        item = SimpleNamespace(sleeping=True, retired=False, wanted_running=True, state="sleeping")

        async def start(**_):
            item.sleeping = False
            entered.set()
            await release.wait()
            item.state = "logged_in"

        item.start = AsyncMock(side_effect=start)
        a = asyncio.create_task(wake(item))
        await entered.wait()
        b = asyncio.create_task(wake(item))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(a, b)
        item.start.assert_awaited_once()

    async def test_cancelled_step_does_not_cancel_shared_wake(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        item = SimpleNamespace(sleeping=True, retired=False, wanted_running=True, state="sleeping")

        async def start(**_):
            item.sleeping = False
            entered.set()
            await release.wait()
            item.state = "logged_in"

        item.start = AsyncMock(side_effect=start)
        with patch("backend.browser_sleep.remember") as saved:
            a = asyncio.create_task(wake(item))
            await entered.wait()
            b = asyncio.create_task(wake(item))
            await asyncio.sleep(0)
            a.cancel()
            await asyncio.gather(a, return_exceptions=True)
            self.assertFalse(item.collection_wake_task.done())
            self.assertFalse(b.done())
            saved.assert_not_called()
            release.set()
            await asyncio.wait_for(b, 1)
        item.start.assert_awaited_once()
        self.assertEqual(item.state, "logged_in")

    async def test_last_cancelled_step_stops_shared_wake(self):
        entered = asyncio.Event()
        item = SimpleNamespace(sleeping=True, retired=False, wanted_running=True, state="sleeping")

        async def start(**_):
            entered.set()
            await asyncio.Event().wait()

        item.start = AsyncMock(side_effect=start)
        with patch("backend.browser_sleep.remember") as saved:
            task = asyncio.create_task(wake(item))
            await entered.wait()
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
            self.assertTrue(item.collection_wake_task.cancelled())
            saved.assert_called_once_with(item, True)

    async def test_inner_response_timeout_is_not_wake_budget_expiry(self):
        from backend.read_errors import PageReadError

        item = SimpleNamespace(
            sleeping=True,
            retired=False,
            wanted_running=True,
            state="sleeping",
            start=AsyncMock(side_effect=TimeoutError()),
        )
        with patch("backend.browser_sleep.remember") as saved:
            with self.assertRaisesRegex(PageReadError, "页面响应超时") as raised:
                await wake(item)
            self.assertNotIn("90 秒", str(raised.exception))
            saved.assert_called_once_with(item, True)

        async def slow_start(**_):
            await asyncio.sleep(1)

        item.start = AsyncMock(side_effect=slow_start)
        real_timeout = asyncio.timeout
        with (
            patch("backend.browser_sleep.remember"),
            patch("backend.browser_sleep.asyncio.timeout", side_effect=lambda _: real_timeout(0.01)),
        ):
            with self.assertRaisesRegex(PageReadError, "超过 90 秒"):
                await wake(item)


class CenterControlTests(unittest.TestCase):
    def test_offline_command_durable_and_replayed_after_reconnection(self):
        from backend.center.database import Database
        from backend.center.home_collection_control import install

        with tempfile.TemporaryDirectory() as folder:
            db = Database("sqlite:///" + str(Path(folder) / "center.db"))
            a = db.register("a")
            b = db.register("b")

            class Hub:
                peers = {a["id"]: object()}
                states = {}

                async def request(self, node, method, path, query="", body=b""):
                    if method == "PUT":
                        self.states[node] = json.loads(body)["paused"]
                    return {
                        "status": 200,
                        "content": json.dumps({"paused": self.states.get(node, False), "confirmed": True}).encode(),
                    }

            hub = Hub()
            app = FastAPI()
            install(app, db, hub, lambda: None)
            with TestClient(app) as client:
                result = client.put("/api/system/home-collection", json={"paused": True}).json()
                self.assertEqual(result["pending"], 1)
                self.assertTrue(hub.states[a["id"]])
                hub.peers[b["id"]] = object()
                # A new center process reconstructs the desired command from its database.
            again = FastAPI()
            install(again, db, hub, lambda: None)
            with TestClient(again) as client:
                for _ in range(20):
                    if hub.states.get(b["id"]) is True:
                        break
                    time.sleep(0.01)
                self.assertTrue(hub.states[b["id"]])
                result = client.put("/api/system/home-collection", json={"paused": False}).json()
                self.assertEqual(result["pending"], 0)
                self.assertFalse(hub.states[a["id"]])
                self.assertFalse(hub.states[b["id"]])
            db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
