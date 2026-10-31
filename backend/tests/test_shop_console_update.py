import json, tempfile, unittest, time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend.shop_name import resolve, complete
from backend.store import Store


class NameTests(unittest.IsolatedAsyncioTestCase):
    async def test_masked_header_uses_verified_identity(self):
        item = SimpleNamespace(
            shop={},
            evaluate=AsyncMock(
                return_value=json.dumps(
                    {"success": True, "result": {"queryDetailResult": {"mallName": "完整官方旗舰店"}}}
                )
            ),
        )
        self.assertEqual(await resolve(item, "完***店"), "完整官方旗舰店")
        self.assertFalse(complete("完＊店"))

    async def test_unavailable_identity_preserves_good_name_and_throttles(self):
        item = SimpleNamespace(shop={"detected_name": "原完整店铺"}, evaluate=AsyncMock(side_effect=ValueError()))
        self.assertEqual(await resolve(item, "原***铺"), "原完整店铺")
        await resolve(item, "原***铺")
        self.assertEqual(item.evaluate.await_count, 1)

    async def test_repair_masked_alias_but_keep_custom_name(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            a = store.add("one", "pw", name="蓝***店")
            b = store.add("two", "pw", name="自定义简称")
            store.rename(a, "完整店铺")
            store.rename(b, "另一完整店铺")
            shops = {s["id"]: s for s in store.list()}
            self.assertEqual(shops[a]["name"], "完整店铺")
            self.assertEqual(shops[b]["name"], "自定义简称")
            store.rename(a, "错***店")
            self.assertEqual(store.list()[0]["detected_name"], "完整店铺")


class GuiTests(unittest.TestCase):
    def test_panels_fit_and_selection_starts_fetch_immediately(self):
        import tkinter as tk
        from backend.collector_launcher import console_panels

        root = tk.Tk()
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as directory:
                panes, overview, logs = console_panels(root, directory, lambda: [])
                self.assertEqual(overview.frame.cget("text"), "采集运行概况")
                self.assertEqual(logs.frame.cget("text"), "运行日志")
                self.assertEqual(
                    [logs.notebook.tab(tab, "text") for tab in logs.notebook.tabs()], ["操作日志", "运行日志"]
                )
                self.assertFalse(overview.frame.winfo_manager())
                with patch.object(overview, "task_tick") as tick:
                    overview.select("running")
                    tick.assert_called_once()
                logs.reader.stop()
                logs.reader.thread.join(2)
                logs.operations.reader.stop()
                logs.operations.reader.thread.join(2)
        finally:
            root.destroy()
