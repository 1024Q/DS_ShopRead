import asyncio, json, time, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend.remote_control import Control, CDP, dispatch, serve, bridge
from starlette.websockets import WebSocketDisconnect


class Viewer:
    def __init__(self):
        self.messages = []
        self.incoming = asyncio.Queue()
        self.close = AsyncMock()
        self.accept = AsyncMock()

    async def send_json(self, value):
        self.messages.append(value)

    async def receive_text(self):
        value = await self.incoming.get()
        if isinstance(value, Exception):
            raise value
        return json.dumps(value)


class RemoteControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_binary_frames_and_hidden_viewer(self):
        import base64, struct

        c = self.make()
        c.binary_frames = True
        c.ws.send_bytes = AsyncMock()
        c.paused = True
        task = asyncio.create_task(c.frames())
        try:
            c.frame({"data": base64.b64encode(b"jpeg").decode()})
            await asyncio.sleep(0.12)
            c.ws.send_bytes.assert_not_awaited()
            c.paused = False
            c.frame({"data": base64.b64encode(b"jpeg").decode()})
            await asyncio.sleep(0.12)
            packet = c.ws.send_bytes.await_args.args[0]
            size = struct.unpack("!I", packet[:4])[0]
            self.assertEqual(json.loads(packet[4 : 4 + size])["type"], "frame")
            self.assertEqual(packet[4 + size :], b"jpeg")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def make(self):
        ws = Viewer()
        item = SimpleNamespace(
            shop={"id": 7},
            control_until=0,
            retired=False,
            lock=asyncio.Lock(),
            target=AsyncMock(return_value={"id": "page", "webSocketDebuggerUrl": "ws://localhost/test"}),
        )
        service = SimpleNamespace(cancel_shop=AsyncMock(), wake=asyncio.Event())
        control = Control(ws, item, service)
        control.cdp = SimpleNamespace(call=AsyncMock(return_value={}))
        return control

    async def test_frames_wait_for_ack_and_only_keep_latest(self):
        c = self.make()
        task = asyncio.create_task(c.frames())
        try:
            c.frame({"data": "first"})
            await asyncio.sleep(0.12)
            self.assertEqual(len(c.ws.messages), 1)
            c.frame({"data": "obsolete"})
            c.frame({"data": "latest"})
            await asyncio.sleep(0.12)
            self.assertEqual(len(c.ws.messages), 1)
            c.ack.set()
            await asyncio.sleep(0.12)
            self.assertEqual([m["data"] for m in c.ws.messages], ["first", "latest"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_input_does_not_wait_for_frame_ack_and_is_ordered(self):
        c = self.make()
        c.ack.clear()
        task = asyncio.create_task(c.receive())
        try:
            await c.ws.incoming.put(
                {
                    "type": "input",
                    "id": 2,
                    "events": [
                        {"type": "mousePressed", "x": 1, "y": 2},
                        {"type": "mouseMoved", "x": 8, "y": 2},
                        {"type": "mouseReleased", "x": 8, "y": 2},
                    ],
                }
            )
            await asyncio.sleep(0.03)
            self.assertEqual(
                [call.args[1]["type"] for call in c.cdp.call.await_args_list],
                ["mousePressed", "mouseMoved", "mouseReleased"],
            )
            self.assertFalse(c.pressed)
            self.assertEqual(c.ws.messages[-1], {"type": "input_ack", "id": 2})
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_two_frame_window_is_bounded_and_cumulative_ack_unblocks_latest(self):
        c = self.make()
        c.frame_window = 2
        tasks = [asyncio.create_task(c.frames()), asyncio.create_task(c.receive())]
        try:
            c.frame({"data": "first"})
            await asyncio.sleep(0.02)
            self.assertEqual(len(c.ws.messages), 1)  # No unconditional initial frame delay.
            c.frame({"data": "second"})
            await asyncio.sleep(0.1)
            self.assertEqual(len(c.ws.messages), 2)  # Second frame overlaps first ACK.
            for number in range(100):
                c.frame({"data": str(number)})
            await asyncio.sleep(0.1)
            self.assertEqual(len(c.ws.messages), 2)
            self.assertEqual(len(c.inflight), 2)
            await c.ws.incoming.put({"type": "ack", "id": 999999})
            await asyncio.sleep(0.02)
            self.assertEqual(len(c.ws.messages), 2)
            await c.ws.incoming.put({"type": "ack", "id": 2})
            await asyncio.sleep(0.02)
            self.assertEqual(c.ws.messages[-1]["data"], "99")
            self.assertEqual(list(c.inflight), [3])
            self.assertEqual(c.dropped, 99)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_invalid_input_rejected(self):
        c = self.make()
        for event in (
            {"type": "execute", "code": "bad"},
            {"type": "text", "text": "x" * 1025},
            {"type": "mousePressed", "x": float("nan")},
            {"type": "key", "key": "F12"},
        ):
            with self.assertRaises(ValueError):
                await dispatch(c.cdp, event)
        c.cdp.call.assert_not_awaited()

    async def test_unsupported_stream_falls_back(self):
        c = self.make()

        async def call(method, params=None):
            if method == "Page.startScreencast":
                raise ValueError("unsupported")
            return {}

        c.cdp.call.side_effect = call
        await c.configure("smooth")
        self.assertEqual(c.mode, "snapshot")
        self.assertEqual(c.ws.messages[-1]["mode"], "snapshot")

    async def test_hidden_window_without_frames_uses_snapshot(self):
        c = self.make()
        c.target_id = "page"
        c.started = time.monotonic() - 10
        c.cdp.call.return_value = {"data": "fallback"}
        task = asyncio.create_task(c.heartbeat())
        try:
            await asyncio.sleep(0.8)
            self.assertEqual(c.mode, "snapshot")
            self.assertEqual(c.latest["data"], "fallback")
            self.assertGreater(c.item.control_until, time.time())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_same_quality_does_not_restart_stream(self):
        c = self.make()
        await c.configure("balanced")
        calls = c.cdp.call.await_count
        await c.configure("balanced")
        self.assertEqual(c.cdp.call.await_count, calls)

    async def test_missing_damage_frame_after_input_gets_repaired(self):
        c = self.make()
        c.target_id = "page"
        c.last_frame = time.monotonic() - 4
        c.last_action = time.monotonic() - 2
        c.cdp.call.return_value = {"data": "after-input"}
        task = asyncio.create_task(c.heartbeat())
        try:
            await asyncio.sleep(0.8)
            self.assertEqual(c.latest["data"], "after-input")
            self.assertEqual(c.repaired_action, c.last_action)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_failure_restores_collection_lease(self):
        c = self.make()
        c.item.target.side_effect = ValueError("closed")
        with self.assertRaises(ValueError):
            await c.run()
        c.business.cancel_shop.assert_awaited_once_with(7)
        self.assertEqual(c.item.control_until, 0)
        self.assertTrue(c.business.wake.is_set())

    async def test_second_controller_cannot_take_over(self):
        c = self.make()
        c.item.remote_control_active = True
        await serve(c.ws, c.item, c.business)
        c.ws.close.assert_awaited_once_with(code=4409)
        c.business.cancel_shop.assert_not_awaited()

    async def test_authentication_refresh_does_not_change_remote_window_visibility(self):
        from backend.browser import Session

        item = SimpleNamespace(remote_control_active=True, manual_visibility=None, window=AsyncMock())
        await Session.auto_window(item, True)
        await Session.auto_window(item, False)
        item.window.assert_not_awaited()
        item.remote_control_active = False
        await Session.auto_window(item, False)
        item.window.assert_awaited_once_with(False, activate=False)

    async def test_disconnect_releases_drag_and_screencast(self):
        c = self.make()

        class Socket:
            def __init__(self):
                self.queue = asyncio.Queue()
                self.commands = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await self.queue.get()

            async def send(self, raw):
                message = json.loads(raw)
                self.commands.append(message)
                await self.queue.put(json.dumps({"id": message["id"], "result": {}}))

        socket = Socket()
        with patch("backend.remote_control.websockets.connect", return_value=socket):
            task = asyncio.create_task(c.run())
            await asyncio.sleep(0.03)
            await c.ws.incoming.put({"type": "input", "id": 1, "events": [{"type": "mousePressed", "x": 4, "y": 6}]})
            await asyncio.sleep(0.03)
            await c.ws.incoming.put(WebSocketDisconnect())
            with self.assertRaises(WebSocketDisconnect):
                await asyncio.wait_for(task, 2)
        mouse = [m["params"]["type"] for m in socket.commands if m["method"] == "Input.dispatchMouseEvent"]
        self.assertEqual(mouse, ["mousePressed", "mouseReleased"])
        self.assertEqual(c.item.control_until, 0)
        self.assertEqual(socket.commands[-1]["method"], "Page.stopScreencast")


class RemoteRouteTests(unittest.TestCase):
    def test_websocket_auth_and_local_collector_control(self):
        from fastapi.testclient import TestClient
        from backend import main

        client = TestClient(main.app)
        with self.assertRaises(WebSocketDisconnect):
            with client.websocket_connect("/api/shops/1/control"):
                pass

        item = SimpleNamespace()

        async def serve(ws, session, business):
            self.assertIs(session, item)
            await ws.accept()
            await ws.send_json({"shop": 7})
            await ws.close()

        with (
            patch.object(main, "session", return_value=item),
            patch("backend.remote_control.serve", side_effect=serve) as served,
        ):
            with client.websocket_connect(
                "/api/shops/7/control", headers={"Authorization": "Bearer " + main.TOKEN}
            ) as ws:
                self.assertEqual(ws.receive_json(), {"shop": 7})
            served.assert_awaited_once()
        client.close()


if __name__ == "__main__":
    unittest.main()


class MaximizeTests(unittest.IsolatedAsyncioTestCase):
    async def test_hidden_window_stays_hidden_and_uses_window_state(self):
        from backend.browser import Session

        item = SimpleNamespace(
            retired=False,
            state="logged_in",
            visible=False,
            command=AsyncMock(return_value={"windowId": 8}),
            window=AsyncMock(),
        )
        await Session.maximize(item)
        self.assertEqual(item.command.await_args_list[1].args[1], {"windowId": 8, "bounds": {"windowState": "normal"}})
        self.assertEqual(
            item.command.await_args_list[2].args[1], {"windowId": 8, "bounds": {"windowState": "maximized"}}
        )
        item.window.assert_awaited_once_with(False, activate=False)

    async def test_failed_maximize_restores_hidden_state(self):
        from backend.browser import Session

        item = SimpleNamespace(
            retired=False,
            state="logged_in",
            visible=False,
            command=AsyncMock(side_effect=[{"windowId": 8}, {}, ValueError("failed")]),
            window=AsyncMock(),
        )
        with self.assertRaises(ValueError):
            await Session.maximize(item)
        item.window.assert_awaited_once_with(False, activate=False)

    async def test_stopped_browser_is_not_started(self):
        from backend.browser import Session

        item = SimpleNamespace(retired=False, state="stopped", command=AsyncMock())
        with self.assertRaises(ValueError):
            await Session.maximize(item)
        item.command.assert_not_awaited()
