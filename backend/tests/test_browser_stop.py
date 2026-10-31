import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from backend.browser import Session


class StopTests(unittest.IsolatedAsyncioTestCase):
    async def check_stop(self, responses, closes=True):
        item = object.__new__(Session)
        item.port = 9999
        item.state = "logged_in"
        item.visible = True
        item.authenticated = True
        item.error = ""
        item.window_controller = None
        item.close_cdp = AsyncMock()
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.get.side_effect = responses
        socket = AsyncMock()
        socket.recv.side_effect = ConnectionError("closed during shutdown")
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=socket)
        context.__aexit__ = AsyncMock(return_value=False)
        with (
            patch("backend.browser.httpx.AsyncClient", return_value=client),
            patch("backend.browser.websockets.connect", return_value=context),
            patch("backend.browser.asyncio.sleep", new=AsyncMock()),
        ):
            if closes:
                await item.stop()
                self.assertEqual(item.state, "stopped")
                self.assertFalse(item.authenticated)
            else:
                with self.assertRaisesRegex(ValueError, "无法确认"):
                    await item.stop()
                self.assertEqual(item.state, "logged_in")

    async def test_already_closed(self):
        await self.check_stop([httpx.ConnectError("refused")])

    async def test_close_disconnect_then_reset_then_port_closed(self):
        version = httpx.Response(
            200,
            json={"webSocketDebuggerUrl": "ws://127.0.0.1:9999/devtools/browser/test"},
            request=httpx.Request("GET", "http://localhost"),
        )
        await self.check_stop([version, httpx.ReadError("reset"), httpx.ConnectError("refused")])

    async def test_still_running_never_marks_stopped(self):
        version = httpx.Response(
            200,
            json={"webSocketDebuggerUrl": "ws://127.0.0.1:9999/devtools/browser/test"},
            request=httpx.Request("GET", "http://localhost"),
        )
        await self.check_stop([version] * 26, False)
