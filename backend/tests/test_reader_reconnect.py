import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.browser import Session


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    def item(self, directory):
        item = object.__new__(Session)
        item.store = SimpleNamespace(directory=Path(directory))
        item.shop = {"id": 7}
        item.port = 9228
        item.profile = Path(directory) / "profile"
        return item

    async def test_missing_registry_skips_browser_http_entirely(self):
        with tempfile.TemporaryDirectory() as folder:
            item = self.item(folder)
            with (
                patch("backend.collector_lease.owns", return_value=True),
                patch("backend.browser.local_client") as client,
            ):
                await item.reclaim_read_pages()
                client.assert_not_called()

    async def test_reconnect_passes_only_page_ids_and_confirms_owned_close(self):
        def response(value):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: value)

        browser = "ws://127.0.0.1:9228/devtools/browser/11111111-1111-4111-8111-111111111111"
        client = AsyncMock()
        client.get.side_effect = [
            response({"webSocketDebuggerUrl": browser}),
            response(
                [{"type": "page", "id": "owned"}, {"type": "page", "id": "manual"}, {"type": "worker", "id": "worker"}]
            ),
        ]
        socket = AsyncMock()
        socket.recv.return_value = json.dumps({"id": 9999, "result": {"success": True}})
        connection = MagicMock()
        connection.__aenter__ = AsyncMock(return_value=socket)
        connection.__aexit__ = AsyncMock(return_value=False)

        async def cleanup(item, address, targets, close):
            self.assertEqual(address, browser)
            self.assertEqual(targets, {"owned", "manual"})
            await close("owned")
            return {"owned"}

        with (
            tempfile.TemporaryDirectory() as folder,
            patch("backend.collector_lease.owns", return_value=True),
            patch("backend.reader_targets.has_records", return_value=True),
            patch("backend.reader_targets.cleanup", new=AsyncMock(side_effect=cleanup)) as run,
            patch("backend.browser.local_client", return_value=client),
            patch("backend.browser.websockets.connect", return_value=connection),
        ):
            await self.item(folder).reclaim_read_pages()
            run.assert_awaited_once()
            request = json.loads(socket.send.call_args.args[0])
            self.assertEqual(request["params"], {"targetId": "owned"})
            socket.recv.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
