import base64
import json
import tempfile
import threading
import unittest
import uuid
import asyncio
from pathlib import Path
from fastapi.testclient import TestClient
from backend.center.database import Database, jobs
from backend.center.server import create_app
from backend.center.tunnel import ViewerRelay


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_viewer_retains_only_latest_frame_and_preserves_control_messages(self):
        class Socket:
            def __init__(self):
                self.release = asyncio.Event()
                self.started = asyncio.Event()
                self.sent = []

            async def send_bytes(self, data):
                self.started.set()
                await self.release.wait()
                self.sent.append(data)

            async def send_text(self, data):
                self.sent.append(data)

            async def close(self, code):
                pass

        socket = Socket()
        relay = ViewerRelay(socket)
        try:
            relay.offer(b"first", True)
            await socket.started.wait()
            relay.offer('{"type":"input_ack","id":1}')
            for number in range(100):
                relay.offer(str(number).encode(), True)
            self.assertEqual(len(relay.items), 2)
            socket.release.set()
            await asyncio.sleep(0.03)
            self.assertEqual(socket.sent, [b"first", '{"type":"input_ack","id":1}', b"99"])
        finally:
            await relay.close()


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Database("sqlite:///" + str(Path(self.directory.name) / "test.db"))
        self.node = self.db.register("远程测试节点")
        self.db.ingest(
            self.node["id"],
            [{"shop": "7", "scope": "shop_metadata", "updated": 1, "payload": {"name": "店铺", "platform": "pdd"}}],
        )
        self.shop = self.db.shops()[0]["id"]
        self.admin = {"Authorization": "Bearer " + "x" * 40}
        self.agent = {"Authorization": "Bearer " + self.node["token"], "X-Node-ID": self.node["id"]}
        self.client = TestClient(create_app(self.db, "x" * 40))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.db.engine.dispose()
        self.directory.cleanup()

    def test_log_reads_are_live_and_never_cached_in_center_database(self):
        from backend.center.database import snapshots

        with self.client.websocket_connect("/agent/tunnel", headers=self.agent) as node:
            for _ in range(2):
                results = []
                thread = threading.Thread(
                    target=lambda: results.append(self.client.get("/api/logs", headers=self.admin))
                )
                thread.start()
                request = node.receive_json()
                self.assertEqual(request["path"], "logs")
                payload = json.dumps({"rows": [{"shop_id": 7, "message": "file only"}]}).encode()
                node.send_json(
                    {
                        "type": "response",
                        "id": request["id"],
                        "status": 200,
                        "body": base64.b64encode(payload).decode(),
                        "headers": {"content-type": "application/json"},
                    }
                )
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results[0].status_code, 200)
                self.assertEqual(results[0].json()["rows"][0]["shop_id"], self.shop)
        self.assertFalse(any(r["scope"].startswith("response:") for r in self.db.list(snapshots)))

    def test_full_source_snapshot_maps_ids_without_per_field_database_queries(self):
        from unittest.mock import patch

        with self.client.websocket_connect("/agent/tunnel", headers=self.agent) as node:
            results = []
            with patch.object(self.db, "shops", wraps=self.db.shops) as shops:
                thread = threading.Thread(
                    target=lambda: results.append(
                        self.client.get("/api/system/sources?snapshot=true", headers=self.admin)
                    )
                )
                thread.start()
                request = node.receive_json()
                payload = json.dumps(
                    {
                        "snapshot_complete": True,
                        "rows": [
                            {
                                "shop_id": 7,
                                "kind": "orders",
                                "scope": {"status": "待收货"},
                                "delivery": {"state": "confirmed"},
                            }
                            for _ in range(500)
                        ],
                    }
                ).encode()
                node.send_json(
                    {
                        "type": "response",
                        "id": request["id"],
                        "status": 200,
                        "body": base64.b64encode(payload).decode(),
                        "headers": {"content-type": "application/json"},
                    }
                )
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results[0].status_code, 200)
                self.assertEqual(len(results[0].json()["rows"]), 500)
                self.assertTrue(all(r["shop_id"] == self.shop for r in results[0].json()["rows"]))
                self.assertLessEqual(shops.call_count, 3)

    def test_binary_frames_and_inputs_route_to_local_shop(self):
        with self.client.websocket_connect("/agent/tunnel", headers=self.agent) as node:
            with self.client.websocket_connect(f"/api/shops/{self.shop}/control", headers=self.admin) as viewer:
                self.assertEqual(viewer.receive_json(), {"type": "transport", "dedicated": False})
                opened = node.receive_json()
                self.assertEqual(opened["shop"], 7)
                self.assertEqual(opened["type"], "open")
                wire = b"\x00\x00\x00\x02{}\xff\xd8\xff\xd9"
                node.send_bytes(uuid.UUID(hex=opened["id"]).bytes + wire)
                self.assertEqual(viewer.receive_bytes(), wire)
                viewer.send_text('{"type":"ping","at":42}')
                received = node.receive_json()
                self.assertEqual(received["type"], "input")
                self.assertEqual(json.loads(received["data"])["at"], 42)
            self.assertEqual(node.receive_json()["type"], "close")

    def test_dedicated_control_channel_does_not_consume_business_responses(self):
        with self.client.websocket_connect("/agent/tunnel", headers=self.agent) as business:
            with self.client.websocket_connect("/agent/control-tunnel", headers=self.agent) as control:
                with self.client.websocket_connect(f"/api/shops/{self.shop}/control", headers=self.admin) as viewer:
                    self.assertTrue(viewer.receive_json()["dedicated"])
                    opened = control.receive_json()
                    self.assertEqual(opened["type"], "open")
                    results = []
                    thread = threading.Thread(
                        target=lambda: results.append(self.client.get("/api/browsers", headers=self.admin))
                    )
                    thread.start()
                    request = business.receive_json()
                    self.assertEqual(request["type"], "request")
                    business.send_json(
                        {
                            "type": "response",
                            "id": request["id"],
                            "status": 200,
                            "body": base64.b64encode(b"[]").decode(),
                            "headers": {"content-type": "application/json"},
                        }
                    )
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(results[0].status_code, 200)
                    control.send_bytes(uuid.UUID(hex=opened["id"]).bytes + b"frame")
                    self.assertEqual(viewer.receive_bytes(), b"frame")
                    viewer.send_text('{"type":"ping","at":3}')
                    self.assertEqual(control.receive_json()["type"], "input")

    def test_control_channel_requires_node_authentication(self):
        from starlette.websockets import WebSocketDisconnect

        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(
                "/agent/control-tunnel", headers={"X-Node-ID": self.node["id"], "Authorization": "Bearer invalid"}
            ):
                pass

    def test_node_key_cannot_open_administrator_control(self):
        from starlette.websockets import WebSocketDisconnect

        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(f"/api/shops/{self.shop}/control", headers=self.agent):
                pass

    def test_confirmation_command_is_not_queued_for_replay_on_disconnect(self):
        result = []
        with self.client.websocket_connect("/agent/tunnel", headers=self.agent) as node:
            thread = threading.Thread(
                target=lambda: result.append(
                    self.client.post(
                        "/api/invoice-imports/" + "a" * 32 + "/confirm", headers=self.admin, json={"files": []}
                    )
                )
            )
            thread.start()
            message = node.receive_json()
            self.assertEqual(message["method"], "POST")
            self.assertEqual(message["path"], "invoice-imports/" + "a" * 32 + "/confirm")
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0].status_code, 504)
        self.assertEqual(self.db.list(jobs), [])
