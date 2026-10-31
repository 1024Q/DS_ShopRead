import asyncio
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend import products, collection_checkpoint as checkpoint
from backend.business_reader import ReadPage
from backend.read_budget import current as read_budget
from backend.read_errors import PageReadError
from backend.store import Store


def response(number=1, total=2, **scope):
    return {
        "success": True,
        "_request": {"page": number, "size": 1, **scope},
        "result": {"goods_list": [{"id": number}], "total": total},
    }


NODE = shutil.which("node")
if not NODE:
    bundled_node = Path(__file__).resolve().parents[2] / ".runtime/node.exe"
    if bundled_node.is_file():
        NODE = str(bundled_node)


class NativeProductsPage:
    """Execute the real callback script while React commits after each response."""

    def __init__(self, total=167, size=10, props=None):
        self.total = total
        self.size = size
        self.products = asyncio.Queue()
        self.probes = []
        self.calls = []
        self.pending = {"current": 1, "total": total, "pageSize": size}
        self.props = dict(self.pending if props is None else props)
        self.products.put_nowait(self.response(1))

    def response(self, number):
        value = response(number, self.total, size=self.size)
        value["result"]["goods_list"] = [
            {"id": n} for n in range((number - 1) * self.size + 1, min(number * self.size, self.total) + 1)
        ]
        return value

    async def evaluate(self, script):
        if "p.onChange(" not in script:
            if "e.click()" in script:
                self.products.put_nowait(self.response(1))
            return True
        fixture = "const calls=[];const props=" + json.dumps(self.props) + ";"
        fixture += "props.onChange=(number,size)=>calls.push([number,size]);global.document={querySelector:()=>({offsetWidth:100,__reactFiber$test:{memoizedProps:props}})};"
        fixture += "const ready=" + script + ";process.stdout.write(JSON.stringify({ready,calls}));"
        result = subprocess.run(
            [NODE, "-e", fixture], capture_output=True, text=True, encoding="utf-8", timeout=10, check=True
        )
        actual = json.loads(result.stdout)
        self.probes.append((dict(self.props), actual))
        if actual["calls"]:
            for number, size in actual["calls"]:
                self.calls.append(number)
                self.products.put_nowait(self.response(number))
                self.pending = {"current": number, "total": self.total, "pageSize": self.size}
        else:
            self.props = dict(self.pending)
        return actual["ready"]


@unittest.skipUnless(NODE, "Node is required to execute the native pagination fixture")
class NativePaginationRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_waits_for_all_filter_then_received_tail_and_head_check(self):
        page = NativeProductsPage(props={"current": 1, "total": 37, "pageSize": 10})
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("test", "pw")
            async with checkpoint.async_context(
                store, sid, "products:test", {"batch_pages": 12, "batch_seconds": 60}
            ) as context:
                key = hashlib.sha256(b"products:all:v1").hexdigest()
                saved_rows = {str(n): {"id": str(n)} for n in range(1, 131)}
                part = {
                    "head": checkpoint.signature({"ids": [str(n) for n in range(1, 11)]}),
                    "tail": checkpoint.signature({"ids": [str(n) for n in range(121, 131)]}),
                    "next": 14,
                    "rows": saved_rows,
                    "ids": list(saved_rows),
                    "loaded": 130,
                    "total": 167,
                    "done": False,
                }
                context.data["parts"][key] = part
                context.save()
            async with checkpoint.async_context(
                store, sid, "products:test", {"batch_pages": 12, "batch_seconds": 60}
            ) as context:
                with patch("backend.products.asyncio.sleep", AsyncMock()):
                    result = await products.collect(page)
                self.assertTrue(context.data["parts"][key]["done"])
                self.assertEqual(context.data["parts"][key]["loaded"], 167)
            self.assertTrue(result["complete"])
            self.assertEqual(len(result["rows"]), 167)
            self.assertEqual(page.calls, [13, 14, 15, 16, 17, 1])
            self.assertEqual(
                page.probes[0], ({"current": 1, "total": 37, "pageSize": 10}, {"ready": False, "calls": []})
            )
            self.assertEqual(page.probes[1][1]["calls"], [[13, 10]])
            self.assertEqual(
                page.probes[2], ({"current": 1, "total": 167, "pageSize": 10}, {"ready": False, "calls": []})
            )
            self.assertEqual(page.probes[3][1]["calls"], [[14, 10]])
            self.assertEqual(page.probes[-1][0]["current"], 17)
            self.assertEqual(page.probes[-1][1]["calls"], [[1, 10]])

    async def test_sequential_pages_wait_for_current_and_observed_size(self):
        page = NativeProductsPage(total=23, props={"current": 1, "total": 23, "pageSize": 20})
        with patch("backend.products.asyncio.sleep", AsyncMock()) as sleep:
            result = await products.collect(page)
        self.assertEqual(page.calls, [2, 3])
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["rows"]), 23)
        self.assertEqual(page.probes[0][1], {"ready": False, "calls": []})
        self.assertEqual(page.probes[1][1]["calls"], [[2, 10]])
        self.assertEqual(page.probes[2][0]["current"], 1)
        self.assertEqual(page.probes[2][1], {"ready": False, "calls": []})
        self.assertEqual(page.probes[3][1]["calls"], [[3, 10]])
        self.assertEqual(sleep.await_count, 2)

    async def test_out_of_range_page_does_not_invoke_callback(self):
        page = NativeProductsPage(total=23)

        async def fetch_invalid_page(key, fetch, progress):
            await fetch(1)
            return await fetch(4)

        token = checkpoint.current.set(SimpleNamespace())
        try:
            with (
                patch("backend.products.checkpoint.paginate", fetch_invalid_page),
                patch("backend.products.asyncio.sleep", AsyncMock()),
            ):
                with self.assertRaisesRegex(PageReadError, "尚未与已读取列表同步"):
                    await products.collect(page)
        finally:
            checkpoint.current.reset(token)
        self.assertEqual(page.calls, [])
        self.assertEqual(len(page.probes), 20)
        self.assertTrue(all(actual == {"ready": False, "calls": []} for _, actual in page.probes))

    async def test_zero_products_complete_without_needing_pagination_to_render(self):
        page = NativeProductsPage(total=0, props={"current": 1, "total": 37, "pageSize": 10})
        result = await products.collect(page)
        self.assertEqual(result, {"rows": [], "source_total": 0, "complete": True})
        self.assertEqual(page.calls, [])
        self.assertEqual(page.probes, [])


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(NODE, "Node is required to execute product page readiness")
    async def test_products_require_correct_route_and_frame_not_legacy_header(self):
        page = ReadPage(SimpleNamespace())
        page.call = AsyncMock()
        page.evaluate = AsyncMock(return_value={"ready": True, "login": False})
        with patch("backend.business_reader.asyncio.sleep", AsyncMock()):
            await page.open("products")
        expression = page.evaluate.await_args.args[0]
        cases = [
            ("https://mms.pinduoduo.com/goods/goods_list", True, "complete"),
            ("https://mms.pinduoduo.com/home/", True, "complete"),
            ("https://example.com/goods/goods_list", True, "complete"),
            ("https://mms.pinduoduo.com/goods/goods_list", True, "loading"),
            ("https://mms.pinduoduo.com/goods/goods_list", False, "complete"),
            ("https://mms.pinduoduo.com/login", True, "complete"),
        ]
        fixture = (
            "const expression="
            + json.dumps(expression)
            + ";const reuse="
            + json.dumps(page.reuse_ready_expression("products"))
            + ";const cases="
            + json.dumps(cases)
            + ";const values=cases.map(([url,body,readyState])=>{"
            # The new product page has no legacy merchant header. Its actual
            # route, body and loading state determine readiness independently.
            + "global.location=new URL(url);global.performance={timeOrigin:123};"
            + "global.document={body:body?{}:null,readyState,querySelector:()=>null};"
            + "const result=eval(expression);return {ready:!!result.ready,login:result.login,reusable:!!eval(reuse)};"
            + "});process.stdout.write(JSON.stringify(values));"
        )
        result = subprocess.run(
            [NODE, "-e", fixture], capture_output=True, text=True, encoding="utf-8", timeout=10, check=True
        )
        self.assertEqual(
            json.loads(result.stdout),
            [
                {"ready": True, "login": False, "reusable": True},
                *[{"ready": False, "login": False, "reusable": False} for _ in range(4)],
                {"ready": False, "login": True, "reusable": False},
            ],
        )
        self.assertIn(".mms-header__user-info", page.reuse_ready_expression("overview"))

    async def test_login_is_still_a_failure_before_product_wait(self):
        page = ReadPage(SimpleNamespace())
        page.call = AsyncMock()
        page.evaluate = AsyncMock(return_value={"ready": False, "login": True})
        with patch("backend.business_reader.asyncio.sleep", AsyncMock()):
            with self.assertRaisesRegex(PageReadError, "登录已失效"):
                await page.open("products")

    async def test_filter_reset_rejects_late_initial_response(self):
        page = ReadPage(SimpleNamespace())
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def body(*args):
            entered.set()
            await finish.wait()
            return {"body": json.dumps(response())}

        page.call = body
        task = asyncio.create_task(page.read_products("old", 0, {}, 0))
        await entered.wait()
        page.reset_products()
        finish.set()
        await task
        self.assertTrue(page.products.empty())

    async def test_native_paging_ignores_wrong_scope_but_reports_http_error_immediately(self):
        queue = asyncio.Queue()
        queue.put_nowait(response())
        stages = []
        calls = []

        async def evaluate(script):
            if "p.onChange(" in script:
                calls.append("next")
                queue.put_nowait(response(2, is_onsale=1))  # Wrong tab response.
                queue.put_nowait({"success": False, "errorMsg": "商品接口 HTTP 403"})
            elif "e.click()" in script:
                queue.put_nowait(response())
            return True

        page = SimpleNamespace(products=queue, evaluate=evaluate, read_progress=lambda n, t, s: stages.append(s))
        with self.assertRaisesRegex(ValueError, "HTTP 403"):
            await asyncio.wait_for(products.collect(page), 0.5)
        self.assertEqual(calls, ["next"])
        self.assertIn("等待商品第 2 页列表响应", stages)

    async def test_controls_can_render_after_response_without_fixed_initial_sleep(self):
        queue = asyncio.Queue()
        queue.put_nowait(response(total=1))
        probes = 0

        async def evaluate(script):
            nonlocal probes
            if "e.click()" in script:
                queue.put_nowait(response(total=1))
                return True
            probes += 1
            return probes > 1

        with patch("backend.products.asyncio.sleep", AsyncMock()) as sleep:
            result = await products.collect(SimpleNamespace(products=queue, evaluate=evaluate))
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["rows"]), 1)
        sleep.assert_awaited_once_with(0.2)

    async def test_insufficient_budget_saves_demand_without_starting_a_query_or_claiming_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("test", "pw")
            async with checkpoint.async_context(store, sid, "products:test", {"batch_pages": 12, "batch_seconds": 20}):
                token = read_budget.set(SimpleNamespace(seconds=40, remaining=lambda: 0.2, database=nullcontext))
                page = SimpleNamespace(products=asyncio.Queue(), evaluate=AsyncMock())
                try:
                    with self.assertRaises(checkpoint.YieldCollection):
                        await products.collect(page)
                finally:
                    read_budget.reset(token)
                page.evaluate.assert_not_awaited()
            with store.connect() as db:
                row = db.execute("SELECT payload FROM collection_checkpoints WHERE shop_id=?", (sid,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(json.loads(row[0])["parts"], {})

    async def test_budget_limited_response_wait_yields_and_retains_saved_rows(self):
        save = AsyncMock()
        context = SimpleNamespace(save=lambda: None)
        cp_token = checkpoint.current.set(context)
        budget_token = read_budget.set(SimpleNamespace(seconds=0.1, remaining=lambda: 0.02))
        try:
            with patch("backend.products.database_work", save):
                with self.assertRaises(checkpoint.YieldCollection):
                    await products.step(SimpleNamespace(), asyncio.Queue().get, "等待商品第 2 页列表响应")
            save.assert_awaited_once_with(context.save)
        finally:
            checkpoint.current.reset(cp_token)
            read_budget.reset(budget_token)

    async def test_regular_interface_timeout_is_reported_at_actual_step(self):
        with self.assertRaisesRegex(PageReadError, "等待商品第 2 页列表响应超时"):
            await products.step(SimpleNamespace(), asyncio.Queue().get, "等待商品第 2 页列表响应", 0.01)
