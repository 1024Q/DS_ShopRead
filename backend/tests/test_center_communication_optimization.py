import asyncio
import json
import os
import tempfile
import time
import tracemalloc
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from sqlalchemy import update
from backend.center.database import Database, jobs, snapshots, nodes
from backend.center.download_stream import DownloadStream
from backend.center.tunnel_protocol import CHUNK_BYTES, REQUEST_LIMIT
from backend.center.agent_loops import periodic
from backend.center.worker_guard import require_single_worker


class DatabaseOptimizations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database("sqlite:///" + str(Path(self.temp.name) / "center.db"))
        self.node = self.db.register("test")

    def tearDown(self):
        self.db.engine.dispose()
        self.temp.cleanup()

    def test_cached_auth_rejects_wrong_token_and_invalidation_revokes(self):
        node, token = self.node["id"], self.node["token"]
        self.assertTrue(self.db.authenticate(node, token))
        self.assertFalse(self.db.authenticate(node, "wrong"))
        with self.db.engine.begin() as db:
            db.execute(update(nodes).where(nodes.c.id == node).values(token_hash=""))
        self.assertFalse(self.db.authenticate(node, token, fresh=True))
        self.db.invalidate_node(node)
        self.assertFalse(self.db.authenticate(node, token))
        self.assertIsNone(self.db.resolve_node(1))

    def test_retry_has_backoff_and_stops_without_deleting_data(self):
        node = self.node["id"]
        self.db.ingest(
            node,
            [
                {
                    "shop": "0",
                    "scope": "collector_status",
                    "updated": time.time(),
                    "payload": {"capabilities": ["list-completion-v1"]},
                }
            ],
        )
        jid = self.db.enqueue(node, {"kind": "orders", "shop_id": 1, "_list_completion": {"version": 1, "id": "test"}})
        for attempt in range(20):
            with self.db.engine.begin() as db:
                db.execute(update(jobs).where(jobs.c.id == jid).values(expires=0))
            claimed = self.db.claim(node)
            self.assertIsNotNone(claimed)
            self.assertTrue(self.db.finish(node, jid, claimed["lease"], {"ok": False, "message": "permission"}))
            row = self.db.list(jobs)[0]
            self.assertEqual(row["result"]["attempts"], attempt + 1)
            if attempt < 19:
                self.assertEqual(row["state"], "queued")
                self.assertGreaterEqual(row["expires"], time.time() + 58)
        self.assertEqual(row["state"], "failed")
        self.assertTrue(row["result"]["retry_exhausted"])
        self.assertIsNone(self.db.claim(node))
        self.assertTrue(self.db.list(snapshots))

    def test_clock_offset_and_transfer_does_not_fence_slower_target(self):
        node = self.node["id"]
        dest = self.db.register("dest")["id"]
        self.db.ingest(
            node,
            [
                {
                    "shop": "0",
                    "scope": "collector_status",
                    "updated": time.time(),
                    "payload": {"server_time": time.time() + 120},
                },
                {
                    "shop": "1",
                    "scope": "shop_metadata",
                    "updated": 2000,
                    "payload": {"platform": "pdd", "name": "test"},
                },
                {"shop": "1", "scope": "overview", "updated": 2000, "payload": {"metrics": {"sales": 1}}},
            ],
        )
        status = self.db.list(snapshots, shop="0")[0]["payload"]
        self.assertTrue(status["clock_warning"])
        shop = self.db.shops()[0]
        self.db.move_shop(shop, dest, 2, {"name": "test", "platform": "pdd", "profile_version": 0})
        self.db.ingest(
            dest, [{"shop": "2", "scope": "overview", "updated": 1000, "payload": {"metrics": {"sales": 2}}}]
        )
        self.assertEqual(self.db.list(snapshots, node=dest, scope="overview")[0]["payload"]["metrics"]["sales"], 2)
        self.db.ingest(dest, [{"shop": "2", "scope": "overview", "updated": 900, "payload": {"metrics": {"sales": 9}}}])
        self.assertEqual(self.db.list(snapshots, node=dest, scope="overview")[0]["payload"]["metrics"]["sales"], 2)
        self.db.ingest(
            node, [{"shop": "1", "scope": "overview", "updated": 3000, "payload": {"metrics": {"sales": 3}}}]
        )
        self.assertEqual(self.db.list(snapshots, node=dest, scope="overview")[0]["payload"]["metrics"]["sales"], 2)

    def test_workers_fail_closed(self):
        with patch.dict(os.environ, {"WEB_CONCURRENCY": "2"}):
            with self.assertRaises(RuntimeError):
                require_single_worker()


class AsyncOptimizations(unittest.IsolatedAsyncioTestCase):
    async def test_slow_metadata_does_not_delay_heartbeat(self):
        beats = []

        async def blocked():
            await asyncio.Event().wait()

        async def beat():
            beats.append(time.monotonic())

        tasks = [
            asyncio.create_task(periodic(blocked, 0.01, "元数据")),
            asyncio.create_task(periodic(beat, 0.03, "心跳")),
        ]
        await asyncio.sleep(0.35)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertGreaterEqual(len(beats), 3)

    async def test_download_30mb_is_credit_bounded(self):
        peer = SimpleNamespace(send=AsyncMock(), downloads={})
        stream = DownloadStream(peer, "test")
        peer.downloads["test"] = stream
        count = 0
        tracemalloc.start()

        async def produce():
            for _ in range(120):
                stream.offer(b"x" * CHUNK_BYTES)
                while not stream.queue.empty():
                    await asyncio.sleep(0)
            stream.finish()

        task = asyncio.create_task(produce())
        async for block in stream.iterate():
            count += len(block)
        await task
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        self.assertEqual(count, 30 * 1024 * 1024)
        self.assertLess(peak, 5 * 1024 * 1024)
        self.assertEqual(peer.send.await_count, 121)
        self.assertFalse(peer.downloads)

    async def test_agent_overload_responds_without_waiting_for_local(self):
        from backend.center.agent_tunnel import run

        class Socket:
            def __init__(self):
                self.items = asyncio.Queue()
                self.sent = []
                self.busy = asyncio.Event()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await self.items.get()

            async def send(self, data):
                if isinstance(data, str):
                    self.sent.append(json.loads(data))
                    if self.sent[-1].get("status") == 429:
                        self.busy.set()

        socket = Socket()

        async def block(*a, **kw):
            await asyncio.Event().wait()

        local = SimpleNamespace(request=block, base_url=SimpleNamespace(port=8001))
        with patch("backend.center.agent_tunnel.websockets.connect", return_value=socket):
            task = asyncio.create_task(run("http://test", {}, local, "token"))
            for _ in range(REQUEST_LIMIT + 1):
                await socket.items.put(
                    json.dumps({"type": "request", "id": uuid.uuid4().hex, "method": "GET", "path": "shops"})
                )
            try:
                await asyncio.wait_for(socket.busy.wait(), 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual([x["status"] for x in socket.sent], [429])


class WireStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_headers_chunks_and_ack_do_not_buffer_whole_response(self):
        from backend.center.tunnel import Hub

        class Socket:
            headers = {"x-tunnel-capabilities": "binary-body,chunked-body"}

            def __init__(self):
                self.incoming = asyncio.Queue()
                self.outgoing = asyncio.Queue()

            async def accept(self):
                pass

            async def send_json(self, value):
                await self.outgoing.put(value)

            async def send_bytes(self, value):
                await self.outgoing.put(value)

            async def receive(self):
                return await self.incoming.get()

            async def close(self, code):
                await self.incoming.put({"type": "websocket.disconnect"})

        socket = Socket()
        hub = Hub()
        attach = asyncio.create_task(hub.attach("node", socket))
        self.assertIn("chunked-body", (await socket.outgoing.get())["capabilities"])
        request = asyncio.create_task(hub.request("node", "GET", "exports/" + "a" * 32 + "/download", stream=True))
        sent = await socket.outgoing.get()
        ident = sent["id"]
        self.assertTrue(sent["stream"])
        await socket.incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "type": "response",
                        "id": ident,
                        "status": 200,
                        "chunked": True,
                        "headers": {"content-type": "application/octet-stream"},
                    }
                ),
            }
        )
        response = await request
        self.assertIn("stream", response)
        self.assertNotIn("content", response)

        async def node():
            for value in (b"one", b"two"):
                await socket.incoming.put({"type": "websocket.receive", "bytes": uuid.UUID(hex=ident).bytes + value})
                self.assertEqual((await socket.outgoing.get())["type"], "chunk_ack")
            await socket.incoming.put(
                {"type": "websocket.receive", "text": json.dumps({"type": "download_end", "id": ident})}
            )

        producer = asyncio.create_task(node())
        self.assertEqual([chunk async for chunk in response["stream"]], [b"one", b"two"])
        await producer
        self.assertFalse(hub.peers["node"].downloads)
        await socket.close(1000)
        await attach
