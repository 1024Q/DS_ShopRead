"""First-window activation is controlled by CDP, not Chrome startup defaults."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from backend.browser import Session
from backend.store import Store


class BackgroundStartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        store = Store(self.folder.name)
        store.add("fixture", "pw")
        self.session = Session(store.list()[0], store)
        self.session.profile = Path(self.folder.name) / "profile"
        self.session.window = AsyncMock()

    async def create(self, replies):
        socket = AsyncMock()
        if any(isinstance(r, str) and '"targetId": "home"' in r for r in replies):
            replies += [json.dumps({"id": 2, "result": {"sessionId": "attached"}}), json.dumps({"id": 3, "result": {}})]
        socket.recv.side_effect = replies
        context = AsyncMock()
        context.__aenter__.return_value = socket
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"webSocketDebuggerUrl": "ws://fixture/browser"},
        )
        client = SimpleNamespace(get=AsyncMock(return_value=response))
        with (
            patch("backend.browser.local_client", return_value=client),
            patch("backend.browser.websockets.connect", return_value=context),
            patch("backend.browser.asyncio.sleep", new=AsyncMock()),
        ):
            result = await self.session.create_background_home()
        return result, socket

    async def test_first_window_is_created_in_background_after_ignoring_events(self):
        result, socket = await self.create(
            [
                json.dumps({"method": "Target.targetCreated"}),
                json.dumps({"id": 1, "result": {"targetId": "home"}}),
            ]
        )
        self.assertEqual(result, "home")
        command = json.loads(socket.send.await_args_list[0].args[0])
        self.assertEqual(command["method"], "Target.createTarget")
        self.assertIs(command["params"]["background"], True)
        self.assertIs(command["params"]["newWindow"], True)
        self.assertIs(command["params"]["focus"], False)
        if __import__('os').name == 'nt':
            self.assertLess(command["params"]["left"] + command["params"]["width"], 0)
            self.assertLess(command["params"]["top"] + command["params"]["height"], 0)
        self.assertEqual(command["params"]["url"], "about:blank")
        self.session.window.assert_awaited_once_with(False, activate=False)
        commands = [json.loads(c.args[0]) for c in socket.send.await_args_list]
        self.assertEqual(
            [c["method"] for c in commands], ["Target.createTarget", "Target.attachToTarget", "Page.navigate"]
        )
        self.assertEqual(commands[-1]["sessionId"], "attached")

    async def test_rejected_create_does_not_fall_back_to_foreground_window(self):
        with patch("backend.browser.subprocess.Popen") as launch:
            with self.assertRaisesRegex(ValueError, "后台创建"):
                await self.create([json.dumps({"id": 1, "error": {"message": "rejected"}})])
        launch.assert_not_called()

    async def test_ambiguous_create_does_not_retry_submission(self):
        with patch("backend.browser.subprocess.Popen") as launch:
            with self.assertRaises(OSError):
                await self.create([OSError("disconnected after send")])
        launch.assert_not_called()

    @unittest.skipUnless(__import__("os").name == "nt", "Windows cold launch")
    async def test_cold_start_has_no_url_on_command_line(self):
        self.session.headless = False
        self.session.target = AsyncMock(side_effect=[ValueError("not started"), {}])
        self.session.reclaim_read_pages = AsyncMock()
        self.session.create_background_home = AsyncMock()
        self.session.quiet_window = AsyncMock()
        self.session.refresh = AsyncMock(side_effect=lambda: setattr(self.session, "authenticated", True))
        with (
            patch("backend.browser.chrome_path", return_value="fixture-chrome.exe"),
            patch("backend.browser.subprocess.Popen", return_value=Mock()) as launch,
            patch("backend.browser.asyncio.sleep", new=AsyncMock()),
        ):
            await self.session.start()
        args = launch.call_args.args[0]
        self.assertIn("--no-startup-window", args)
        self.assertFalse(any(str(arg).startswith("https://") for arg in args))
        self.session.create_background_home.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
