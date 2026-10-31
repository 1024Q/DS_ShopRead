import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.business_reader import ReadPage
from backend.read_errors import PageReadError
from backend.trade_font import display_map, RENDER, TEMPLATES


class ReaderLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_socket_close_releases_pending_calls(self):
        async def frames():
            if False:
                yield ""

        page = ReadPage(SimpleNamespace())
        page.ws = frames()
        future = asyncio.get_running_loop().create_future()
        page.pending[1] = future
        await page.receive()
        with self.assertRaisesRegex(PageReadError, "连接已关闭"):
            await asyncio.wait_for(future, 0.1)
        self.assertEqual(page.pending, {})

    async def test_script_syntax_error_does_not_claim_login_expired(self):
        page = ReadPage(SimpleNamespace())
        page.call = AsyncMock(
            return_value={"exceptionDetails": {"exception": {"description": "SyntaxError: bad expression"}}}
        )
        with self.assertRaisesRegex(ValueError, "脚本语法错误") as result:
            await page.evaluate("invalid")
        self.assertNotIsInstance(result.exception, PageReadError)

    async def test_actual_formatted_glyph_script_compiles_as_cdp_expression(self):
        node = shutil.which("node")
        bundled = Path(__file__).parents[2] / ".runtime/node.exe"
        if not node and bundled.exists():
            node = str(bundled)
        if not node:
            self.skipTest("Node.js required to compile the browser expression")
        captured = []

        async def evaluate(expression):
            captured.append(expression)
            return {"\ue000": format(TEMPLATES["0"], "x")}

        mapping = await display_map(SimpleNamespace(evaluate=evaluate), {"sales": "\ue000"})
        self.assertEqual("\ue000".translate(mapping), "0")
        script = "const vm=require('node:vm');new vm.Script(JSON.parse(require('node:fs').readFileSync(0,'utf8')));"
        result = subprocess.run([node, "-e", script], input=json.dumps(captured[0]), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        # The exact previous embedding of the formatted source must fail.
        original = (Path(__file__).parents[1] / "trade_glyphs.js").read_text(encoding="utf-8")
        self.assertTrue(original.rstrip().endswith(";"))
        bad = subprocess.run(
            [node, "-e", script], input=json.dumps("(" + original + ")([])"), text=True, capture_output=True
        )
        self.assertNotEqual(bad.returncode, 0)
