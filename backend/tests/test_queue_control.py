import asyncio, tempfile, time, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend import collection, queue_control
from backend.business import BusinessService
from backend.store import Store
from backend.collection_checkpoint import Checkpoint


class QueueControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("fixture", "pw")
        self.shop = self.store.list()[0]
        self.service = BusinessService(self.store, None)
        self.service.queue_config["max_concurrent"] = 1

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def test_cancel_waiter_preserves_running_data_checkpoint_and_next_cadence(self):
        s = self.service
        gate = asyncio.Event()
        started = asyncio.Event()
        queued_started = asyncio.Event()
        # Use an enabled homepage scope; a manual retry must not override the
        # separately configured pause of non-home historical reports.
        running = (self.sid, "orders:live")
        queued = (self.sid, "overview:pending")
        payload = {"rows": [{"id": "keep"}], "complete": True}
        self.store.write_cache(self.sid, queued[1], payload)
        collection.register(self.store, self.sid, queued[1], "overview", {"trade_range": "realtime"})
        Checkpoint(self.store, self.sid, queued[1], {}).save()

        async def work(key, event):
            item = SimpleNamespace(lock=asyncio.Lock(), shop=self.shop)
            async with s.slot(key, item, s.progress[key]["kind"]):
                event.set()
                await gate.wait()

        for key, kind, scope in [(running, "orders", {}), (queued, "overview", {"trade_range": "realtime"})]:
            s.progress[key] = {
                "kind": kind,
                "phase": "queued",
                "queued_at": time.time(),
                "scope": scope,
                "platform": "pdd",
            }
        s.jobs[running] = asyncio.create_task(work(running, started))
        await asyncio.wait_for(started.wait(), 1)
        s.jobs[queued] = asyncio.create_task(work(queued, queued_started))
        await asyncio.sleep(0.02)
        result = await queue_control.clear(s)
        self.assertEqual(result, {"ok": True, "count": 1, "running": 1})
        self.assertFalse(s.jobs[running].done())
        self.assertFalse(queued_started.is_set())
        self.assertNotIn(queued, s.progress)
        self.assertNotIn(queued, s.jobs)
        self.assertEqual(self.store.read_cache(self.sid, queued[1])["payload"], payload)
        self.assertTrue(collection.state(self.store, self.sid, queued[1])["continuation"])
        deadline = queue_control.held_until(s, queued)
        self.assertGreater(deadline, time.time())
        prepared = (
            {"trade_range": "realtime"},
            queued[1],
            None,
            s.queue_config,
            {"continuation": True, "failures": 0, "retry_at": 0, "last_success": 0},
        )
        item = SimpleNamespace(state="logged_in", retired=False, control_until=0)
        with patch.object(s, "sync", AsyncMock()):
            s.admit(self.shop, item, "overview", prepared, True, False, False)
            self.assertNotIn(queued, s.jobs)
            s.admit(self.shop, item, "overview", prepared, True, True, False)
            self.assertIn(queued, s.due_plans.entries)  # Retry intent waits without a parked worker.
            self.assertNotIn(queued, s.jobs)
            self.assertTrue(s.due_plans.entries[queued][1]["force"])
        with patch("backend.queue_control.time.time", return_value=deadline + 1):
            self.assertEqual(queue_control.held_until(s, queued), 0)
        gate.set()

    async def test_unstarted_worker_has_no_ghost_queue_entry_and_repeat_is_empty(self):
        s = self.service
        key = (self.sid, "products:pending")
        s.progress[key] = {"kind": "products", "phase": "queued", "queued_at": time.time(), "scope": {}}
        s.jobs[key] = asyncio.create_task(asyncio.sleep(100))
        first, second = await asyncio.gather(queue_control.clear(s), queue_control.clear(s))
        self.assertEqual(first["count"] + second["count"], 1)
        self.assertFalse(s.jobs)
        self.assertFalse(s.progress)
        self.assertEqual((await queue_control.clear(s))["count"], 0)


class QueueControlApiTests(unittest.TestCase):
    def test_authenticated_explicit_post_only(self):
        from fastapi.testclient import TestClient
        from backend import main

        service = BusinessService(None, None)
        with patch.object(main, "business", service):
            client = TestClient(main.app)
            self.assertEqual(client.post("/api/system/clear-queue").status_code, 401)
            headers = {"Authorization": "Bearer " + main.TOKEN}
            self.assertEqual(client.get("/api/system/clear-queue", headers=headers).status_code, 405)
            response = client.post("/api/system/clear-queue", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["count"], 0)

    def test_center_single_node_isolation_and_fleet_partial_results(self):
        import base64, json
        from pathlib import Path
        from fastapi.testclient import TestClient
        from backend.center.database import Database
        from backend.center.server import create_app

        with tempfile.TemporaryDirectory() as directory:
            db = Database("sqlite:///" + str(Path(directory) / "center.db"))
            one = db.register("one")
            two = db.register("two")
            with patch("backend.center.report_exports.ROOT", Path(directory) / "exports"):
                client = TestClient(create_app(db, "a" * 40))
            headers = {"Authorization": "Bearer " + "a" * 40}

            async def remote(node, method, path, *args):
                self.assertEqual((method, path), ("POST", "system/clear-queue"))
                value = {"ok": True, "count": 3} if node == one["id"] else {"detail": "接口不存在"}
                return {
                    "status": 200 if node == one["id"] else 404,
                    "body": base64.b64encode(json.dumps(value).encode()).decode(),
                    "headers": {"content-type": "application/json"},
                }

            client.app.state.hub.request = AsyncMock(side_effect=remote)
            try:
                identity = next(n["id"] for n in db.registry() if n["node"] == one["id"])
                result = client.post(f"/api/nodes/{identity}/proxy/system/clear-queue", headers=headers)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["count"], 3)
                self.assertEqual(client.app.state.hub.request.await_count, 1)
                result = client.post("/api/system/clear-queue", headers=headers)
                self.assertEqual(result.status_code, 502)
                self.assertEqual(result.json()["count"], 3)
                self.assertEqual(
                    [(r["node"], r["ok"]) for r in result.json()["results"]], [("one", True), ("two", False)]
                )
            finally:
                client.close()
                db.engine.dispose()
