import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend.business_reader import ReadPage, close_reader_target
from backend.browser import Session
from backend.store import Store


class PreferencesTests(unittest.TestCase):
    def test_visibility_survives_service_recreation_and_delete_cleans_it(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("test", "pw")
            session = Session(store.list()[0], store)
            self.assertIsNone(session.manual_visibility)
            session.remember_visibility(False)
            restarted = Store(folder)
            self.assertIs(Session(restarted.list()[0], restarted).manual_visibility, False)
            session.remember_visibility(True)
            # Silent by default: an explicit "show" lasts only for that browser session.
            self.assertIsNone(Session(restarted.list()[0], restarted).manual_visibility)
            self.assertFalse(restarted.list()[0]["debug_mode"])
            store.set_debug_mode(sid, True)
            debug = Session(restarted.list()[0], restarted)
            self.assertTrue(debug.debug_mode)
            self.assertIsNone(debug.manual_visibility)
            self.assertTrue(restarted.list()[0]["debug_mode"])
            store.delete(sid)
            self.assertIsNone(store.window_preference(sid))
            self.assertFalse(store.debug_mode(sid))


class ReaderLifetimeTests(unittest.IsolatedAsyncioTestCase):
    def setup_io(self, error=False):
        owner = AsyncMock()
        page = AsyncMock()

        def receive():
            sent = json.loads(owner.send.call_args.args[0])
            if sent["method"] == "Target.closeTarget":
                return json.dumps({"id": sent["id"], "result": {"success": True}})
            return json.dumps(
                {"id": 1, "error": {"message": "not supported"}}
                if error
                else {"id": 1, "result": {"targetId": "hidden-id"}}
            )

        owner.recv.side_effect = receive
        client = AsyncMock()

        def response(data):
            r = SimpleNamespace(json=lambda: data)
            r.raise_for_status = lambda: r
            return r

        client.get.side_effect = [
            response({"webSocketDebuggerUrl": "ws://fixture/browser"}),
            response([{"id": "hidden-id", "webSocketDebuggerUrl": "ws://fixture/page"}]),
        ]
        context = AsyncMock()
        context.__aenter__.return_value = client
        return owner, page, context

    async def test_hidden_target_owns_live_socket_until_exit(self):
        owner, page, context = self.setup_io()
        session = SimpleNamespace(port=9000, data_targets=set())
        reader = ReadPage(session)
        with (
            patch("backend.business_reader.httpx.AsyncClient", return_value=context),
            patch("backend.business_reader.websockets.connect", new=AsyncMock(side_effect=[owner, page])),
            patch("backend.reader_targets.register") as register,
            patch.object(reader, "call", new=AsyncMock()),
        ):
            async with reader:
                methods = [c.args[0] for c in reader.call.await_args_list]
                self.assertIn("Runtime.enable", methods)
                params = json.loads(owner.send.call_args.args[0])["params"]
                self.assertTrue(params["hidden"])
                self.assertTrue(params["background"])
                self.assertFalse(params["focus"])
                register.assert_not_called()
                self.assertIn("hidden-id", session.data_targets)
                # Silent mode never activates the native window.
                self.assertNotIn("Page.bringToFront", methods)
                self.assertIn("Emulation.setFocusEmulationEnabled", methods)
                owner.close.assert_not_awaited()
        owner.close.assert_awaited_once()
        page.close.assert_awaited_once()
        self.assertFalse(session.data_targets)

    async def test_setup_failure_destroys_hidden_target(self):
        owner, page, context = self.setup_io()
        reader = ReadPage(SimpleNamespace(port=9000, data_targets=set()))
        with (
            patch("backend.business_reader.httpx.AsyncClient", return_value=context),
            patch(
                "backend.business_reader.websockets.connect",
                new=AsyncMock(side_effect=[owner, RuntimeError("fixture failure")]),
            ),
        ):
            with self.assertRaises(RuntimeError):
                await reader.__aenter__()
        owner.close.assert_awaited_once()
        self.assertFalse(reader.session.data_targets)

    async def test_window_guard_failure_closes_target_before_platform_code(self):
        owner, page, context = self.setup_io()
        session = SimpleNamespace(
            port=9000, data_targets=set(), guard_windows=AsyncMock(side_effect=ValueError("guard failed"))
        )
        reader = ReadPage(session)
        with (
            patch("backend.business_reader.httpx.AsyncClient", return_value=context),
            patch("backend.business_reader.websockets.connect", new=AsyncMock(return_value=owner)),
            patch.object(reader, "call", new=AsyncMock()) as command,
        ):
            with self.assertRaisesRegex(ValueError, "guard failed"):
                await reader.__aenter__()
        session.guard_windows.assert_awaited_once()
        command.assert_not_awaited()
        self.assertFalse(session.data_targets)

    async def test_unsupported_chrome_never_falls_back_to_visible_tab(self):
        owner, page, context = self.setup_io(error=True)
        reader = ReadPage(SimpleNamespace(port=9000, data_targets=set()))
        with (
            patch("backend.business_reader.httpx.AsyncClient", return_value=context),
            patch("backend.business_reader.websockets.connect", new=AsyncMock(return_value=owner)),
        ):
            with self.assertRaisesRegex(ValueError, "无法创建采集页"):
                await reader.__aenter__()
        self.assertEqual(owner.send.await_count, 1)
        owner.close.assert_awaited_once()

    async def test_cancellation_closes_hidden_owner(self):
        owner, page, context = self.setup_io()
        reader = ReadPage(SimpleNamespace(port=9000, data_targets=set()))
        with (
            patch("backend.business_reader.httpx.AsyncClient", return_value=context),
            patch("backend.business_reader.websockets.connect", new=AsyncMock(side_effect=[owner, page])),
            patch.object(reader, "call", new=AsyncMock()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                async with reader:
                    raise asyncio.CancelledError()
        owner.close.assert_awaited_once()
        self.assertFalse(reader.session.data_targets)

    async def test_closed_browser_cleanup_preserves_original_failure(self):
        owner, page, context = self.setup_io()
        reader = ReadPage(SimpleNamespace(port=9000, data_targets=set()))
        with (
            patch("backend.business_reader.httpx.AsyncClient", return_value=context),
            patch("backend.business_reader.websockets.connect", new=AsyncMock(side_effect=[owner, page])),
            patch.object(reader, "call", new=AsyncMock()),
        ):
            with self.assertRaisesRegex(ValueError, "实际失败步骤"):
                async with reader:
                    owner.send.side_effect = OSError("browser closed")
                    raise ValueError("实际失败步骤")
        owner.close.assert_awaited_once()
        self.assertFalse(reader.session.data_targets)

    async def test_direct_reader_reclaims_idle_pages_before_create_and_tracks_close(self):
        owner, page, context = self.setup_io()
        session = SimpleNamespace(port=9000, data_targets=set(), manual_visibility=True)
        reader = ReadPage(session)
        record = {"target_id": "hidden-id"}

        async def idle(_):
            owner.send.assert_not_awaited()
            return 17

        with (
            patch("backend.read_page_pool.release_idle", new=AsyncMock(side_effect=idle)),
            patch("backend.reader_targets.register", return_value=record) as register,
            patch("backend.reader_targets.forget") as forget,
            patch("backend.business_reader.httpx.AsyncClient", return_value=context),
            patch("backend.business_reader.websockets.connect", new=AsyncMock(side_effect=[owner, page])),
            patch.object(reader, "call", new=AsyncMock()),
        ):
            async with reader:
                self.assertEqual(reader._pool_epoch, 17)
                self.assertFalse(reader.hidden_target)
                register.assert_called_once_with(session, "ws://fixture/browser", "hidden-id")
                forget.assert_not_called()
            forget.assert_called_once_with(session, record)

    def test_manual_show_mode_changes_page_pool_identity(self):
        from backend.read_page_pool import identity

        root = SimpleNamespace(shop={"id": 1}, port=9000, state="logged_in", manual_visibility=None)
        hidden = identity(root)
        root.manual_visibility = True
        self.assertNotEqual(identity(root), hidden)

    async def test_screenshot_page_is_created_offscreen_without_focus(self):
        owner, socket, context = self.setup_io()
        class SurfacePage(ReadPage):
            requires_native_surface = True
        session = SimpleNamespace(port=9000, data_targets=set())
        reader = SurfacePage(session)
        with (
            patch("backend.business_reader.httpx.AsyncClient", return_value=context),
            patch("backend.business_reader.websockets.connect", new=AsyncMock(side_effect=[owner, socket])),
            patch("backend.reader_targets.register"),
            patch.object(reader, "call", new=AsyncMock()),
        ):
            async with reader:
                params = json.loads(owner.send.call_args.args[0])["params"]
                self.assertFalse(params["hidden"])
                self.assertTrue(params["newWindow"])
                self.assertFalse(params["focus"])
                if __import__('os').name == 'nt':
                    self.assertLess(params["left"] + params["width"], 0)

    async def test_unconfirmed_close_keeps_record_and_original_read_error(self):
        owner, page, context = self.setup_io()
        reader = ReadPage(SimpleNamespace(port=9000, data_targets=set(), manual_visibility=True))
        with (
            patch("backend.reader_targets.register", return_value={"target_id": "hidden-id"}),
            patch("backend.reader_targets.forget") as forget,
            patch("backend.business_reader.httpx.AsyncClient", return_value=context),
            patch("backend.business_reader.websockets.connect", new=AsyncMock(side_effect=[owner, page])),
            patch.object(reader, "call", new=AsyncMock()),
        ):
            with self.assertRaisesRegex(ValueError, "实际读取错误"):
                async with reader:
                    owner.recv.side_effect = None
                    owner.recv.return_value = json.dumps({"id": 9999, "error": {"message": "not closed"}})
                    raise ValueError("实际读取错误")
            forget.assert_not_called()
            owner.close.assert_awaited_once()

    async def test_ownership_write_failure_closes_new_target_before_navigation(self):
        owner, page, context = self.setup_io()
        reader = ReadPage(SimpleNamespace(port=9000, data_targets=set(), manual_visibility=True))
        with (
            patch("backend.reader_targets.register", side_effect=OSError("full disk")),
            patch("backend.business_reader.httpx.AsyncClient", return_value=context),
            patch("backend.business_reader.websockets.connect", new=AsyncMock(return_value=owner)),
            patch.object(reader, "call", new=AsyncMock()) as command,
        ):
            with self.assertRaisesRegex(OSError, "full disk"):
                await reader.__aenter__()
            command.assert_not_awaited()
            self.assertEqual(json.loads(owner.send.call_args.args[0])["method"], "Target.closeTarget")
            self.assertFalse(reader.session.data_targets)

    async def test_close_ignores_events_until_matching_ack(self):
        socket = AsyncMock()
        socket.recv.side_effect = [
            json.dumps({"method": "Target.targetDestroyed"}),
            json.dumps({"id": 9999, "result": {"success": True}}),
        ]
        await close_reader_target(socket, "owned")
        self.assertEqual(socket.recv.await_count, 2)
