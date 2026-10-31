import asyncio, json, tempfile, time, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend.store import Store
from backend.business import BusinessService, options_for
from backend import attention, collection
from backend.collection_queue import select_jobs
from backend.scheduler import Scheduler
from backend.update_events import events
from backend.tests.collection_fixture import assign, policy


class AttentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("urgent", "pw")
        self.shop = self.store.list()[0]
        self.session = SimpleNamespace(
            shop=self.shop, state="logged_in", retired=False, visible=False, lock=asyncio.Lock(), control_until=0
        )
        self.service = BusinessService(self.store, lambda _: self.session)

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    def payload(self, rows, start=100, complete=True):
        return {"rows": rows, "complete": complete, "_sync": {"snapshot_started_at": start}}

    async def test_atomic_urgent_snapshot_does_not_replace_full_and_clears_resolved(self):
        full = self.payload([{"id": "ordinary", "status": "已完结"}])
        self.store.write_cache(self.sid, "tickets:full", full)
        attention.publish(
            self.store,
            self.sid,
            "tickets",
            self.payload(
                [
                    {"id": "one", "status": "待处理", "attention_status": "2小时内将逾期"},
                    {"id": "two", "status": "已完结"},
                ]
            ),
        )
        result = attention.read(self.service, kind="tickets")
        self.assertEqual([r["id"] for r in result["rows"]], ["one"])
        self.assertEqual(self.store.read_cache(self.sid, "tickets:full")["payload"], full)
        with self.assertRaises(ValueError):
            attention.publish(self.store, self.sid, "tickets", self.payload([], 200, False))
        self.assertEqual(attention.read(self.service, kind="tickets")["total"], 1)
        attention.publish(self.store, self.sid, "tickets", self.payload([], 90))
        self.assertEqual(attention.read(self.service, kind="tickets")["total"], 1)
        attention.reconcile(self.store, self.sid, "tickets", self.payload([{"id": "one", "status": "已完结"}], 110))
        self.assertEqual(attention.read(self.service, kind="tickets")["total"], 0)
        self.assertEqual(attention.read(self.service, platform="taobao")["total"], 0)

    async def test_full_absence_and_older_full_are_not_closure(self):
        attention.publish(
            self.store,
            self.sid,
            "tickets",
            self.payload([{"id": "one", "status": "待处理", "attention_status": "2小时内将逾期"}], 100),
        )
        attention.reconcile(self.store, self.sid, "tickets", self.payload([], 110))
        attention.reconcile(self.store, self.sid, "tickets", self.payload([{"id": "one", "status": "已完结"}], 90))
        self.assertEqual(attention.read(self.service, kind="tickets")["total"], 1)
        attention.publish(self.store, self.sid, "tickets", self.payload([], 120))
        self.assertEqual(attention.read(self.service, kind="tickets")["total"], 0)

    async def test_60s_scopes_distinct_from_normal_and_paused_policy_respected(self):
        config = collection.settings(self.store)
        options = dict(attention.scopes(self.shop))["aftersales"]
        self.assertNotEqual(
            self.service.key("aftersales", options, self.shop),
            self.service.key("aftersales", options_for("aftersales"), self.shop),
        )
        self.assertFalse(collection.due("aftersales", options, 100, config, 159))
        self.assertTrue(collection.due("aftersales", options, 100, config, 160))
        stopped = assign(self.store, {"aftersales@attention": "off"})
        self.assertFalse(collection.due("aftersales", options, 100, stopped, 160))
        assign(self.store, {"aftersales@attention": "realtime"}, reset=True)
        key = self.service.key("aftersales", options, self.shop)
        collection.register(self.store, self.sid, key, "aftersales", options, {"updated": time.time()})
        calls = []
        self.service.enqueue_async = AsyncMock(side_effect=lambda *a, **k: calls.append(a[3]))
        await Scheduler(self.service).tick()
        self.assertFalse(any(o.get("_attention") for o in calls))

    async def test_overdue_order_keeps_shop_lock_without_reserved_priority(self):
        config = policy(max_concurrent=2, shop_concurrent=1)

        def job(kind, urgent=False, phase="queued"):
            return {"kind": kind, "scope": {"_attention": urgent}, "phase": phase, "queued_at": 1}

        jobs = {
            (1, "overview"): job("overview"),
            (2, "tickets"): job("tickets", True),
            (3, "tickets"): job("tickets", True),
        }
        chosen = select_jobs(jobs, config, now=500)
        self.assertEqual(chosen[0], (1, "overview"))
        self.assertIn((1, "overview"), chosen)
        jobs[(2, "other")] = job("overview", phase="running")
        self.assertNotIn((2, "tickets"), select_jobs(jobs, config, now=500))

    async def test_focused_adapters_only_request_target_states(self):
        from backend.operations import collect_operations

        calls = []

        async def page(session, kind, body, *args):
            calls.append(body)
            return {"rows": [], "source_loaded": 0, "source_total": 0, "complete": True}

        with patch("backend.operations.paginate", side_effect=page):
            for kind in ("aftersales", "evidence", "intercepts"):
                calls.clear()
                await collect_operations(self.session, kind, dict(attention.scopes(self.shop))[kind])
                if kind == "aftersales":
                    self.assertEqual([r.get("quickSearchType") for r in calls], [6, 21])
                    self.assertTrue(all("startCreatedTime" not in r for r in calls))
                if kind == "evidence":
                    self.assertEqual([r["status"] for r in calls], [0])
                if kind == "intercepts":
                    self.assertEqual(calls[0]["interceptStatus"], 2)

    async def test_events_emit_committed_revision_and_stop_on_disconnect(self):
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        stream = events(self.store, request, interval=0.001)
        first = await anext(stream)
        self.assertIn("event: database", first)
        heartbeat = await anext(stream)
        self.assertIn("heartbeat", heartbeat)
        self.store.invalidate_responses()
        next_event = await anext(stream)
        self.assertNotEqual(first, next_event)
        request.is_disconnected.return_value = True
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)

    async def test_collection_publishes_only_independent_snapshot(self):
        assign(self.store, {"tickets@attention": "realtime"})
        self.session.read_progress = None
        options = dict(attention.scopes(self.shop))["tickets"]
        key = self.service.key("tickets", options, self.shop)
        self.service.demands.shops[self.sid] = {key: {"sources": {"预警订阅"}}}

        async def collect(*_):
            return self.payload([{"id": "x", "status": "待处理", "attention_status": "2小时内将逾期"}], time.time())

        with patch("backend.business.collect", side_effect=collect):
            await self.service.enqueue_async(self.shop, self.session, "tickets", options, refresh=True)
            await asyncio.gather(*list(self.service.jobs.values()))
        self.assertIsNone(self.store.read_cache(self.sid, key))
        self.assertEqual(attention.read(self.service, kind="tickets")["total"], 1)
        self.assertGreater(collection.state(self.store, self.sid, key)["last_success"], 0)
        from backend.system_status import snapshot

        row = next(r for r in snapshot(self.service)["sources"] if r["kind"] == "tickets")
        self.assertEqual(row["interval_seconds"], 60)
        self.assertEqual(row["loaded_count"], 1)
        self.assertTrue(row["scope"]["_attention"])
        collection.save_settings(self.store, {"settings_patch": {"group_updates": {"realtime": {"seconds": 240}}}})
        self.assertEqual(attention.read(self.service, kind="tickets")["interval_seconds"], 240)
        row = next(r for r in snapshot(self.service)["sources"] if r["kind"] == "tickets")
        self.assertEqual(row["interval_seconds"], 240)

    async def test_authenticated_routes_and_empty_shop_query_contract(self):
        from backend import main
        from fastapi.testclient import TestClient

        async def finite_events(*args):
            yield 'event: database\ndata: {"version":"test:1"}\n\n'

        with (
            patch.object(main, "store", self.store),
            patch.object(main, "business", self.service),
            patch("backend.update_events.events", finite_events),
        ):
            client = TestClient(main.app)
            headers = {"Authorization": "Bearer " + main.TOKEN}
            self.assertEqual(client.get("/api/attention").status_code, 401)
            self.assertEqual(client.get("/api/events").status_code, 401)
            response = client.get("/api/attention?kind=tickets", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["pending"], 1)
            stream = client.get("/api/events", headers=headers)
            self.assertEqual(stream.status_code, 200)
            self.assertIn("text/event-stream", stream.headers["content-type"])
            self.assertIn("test:1", stream.text)
            client.close()

    async def test_remote_node_relays_authenticated_stream_without_buffering_contract(self):
        import httpx
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend import remote_nodes

        app = FastAPI()
        app.include_router(remote_nodes.create_router(self.store, lambda: None))
        token = "test-remote-token-123456"
        with self.store.connect() as db:
            nid = db.execute(
                "INSERT INTO remote_nodes(name,url,token) VALUES(?,?,?)",
                ("test", "http://remote.test", self.store.cipher.encrypt(token.encode())),
            ).lastrowid

        def upstream(request):
            self.assertEqual(request.headers["Authorization"], "Bearer " + token)
            self.assertEqual(request.url.path, "/api/events")
            return httpx.Response(
                200,
                text='event: database\ndata: {"version":"remote:2"}\n\n',
                headers={"content-type": "text/event-stream"},
            )

        original = httpx.AsyncClient

        def client_factory(**kwargs):
            return original(**kwargs, transport=httpx.MockTransport(upstream))

        with patch("backend.remote_nodes.httpx.AsyncClient", client_factory):
            with TestClient(app) as client:
                result = client.get(f"/api/nodes/{nid}/proxy/events")
                self.assertEqual(result.status_code, 200)
                self.assertIn("remote:2", result.text)
                self.assertIn("text/event-stream", result.headers["content-type"])

    async def test_ticket_attention_uses_individually_verified_status_requests(self):
        from backend.business_reader import fetch_list

        self.session.evaluate = AsyncMock(
            return_value=json.dumps({"success": True, "result": {"dataList": [], "total": 0}})
        )
        result = await fetch_list(self.session, "tickets", dict(attention.scopes(self.shop))["tickets"])
        self.assertTrue(result["complete"])
        self.assertEqual(self.session.evaluate.await_count, 1)
        for call, status in zip(self.session.evaluate.await_args_list, (4,)):
            self.assertIn(f'"serviceStatus": [{status}]', call.args[0])
