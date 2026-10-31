import asyncio, unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend.orders import OrdersPage, collect
from backend.read_errors import PageReadError


class TimeoutDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_abort_is_not_misreported_as_login_failure(self):
        from backend.business_reader import ReadPage

        page = ReadPage(SimpleNamespace())
        page.call = AsyncMock(
            return_value={"exceptionDetails": {"exception": {"description": "TimeoutError: signal timed out"}}}
        )
        with self.assertRaisesRegex(PageReadError, "平台接口等待超时"):
            await page.evaluate("fetch")

    async def test_orders_no_unsolicited_landing_response_required(self):
        page = OrdersPage(SimpleNamespace())
        page.initial_order_checked = False
        with patch("backend.orders.collect_single", AsyncMock(return_value={"rows": [], "complete": True})) as reader:
            result = await asyncio.wait_for(collect(page, {"start": 1, "end": 2, "status": ""}), 0.2)
        self.assertTrue(result["complete"])
        reader.assert_awaited_once()

    async def test_old_responses_do_not_extend_order_deadline(self):
        stages = []
        page = OrdersPage(SimpleNamespace(read_progress=lambda n, t, s: stages.append(s)))
        page.evaluate = AsyncMock(return_value=True)
        original = asyncio.timeout

        async def noise():
            while True:
                page.order_responses.put_nowait(({"pageNumber": 99}, {}))
                await asyncio.sleep(0.002)

        noise_task = asyncio.create_task(noise())
        try:
            with patch("backend.orders.asyncio.timeout", side_effect=lambda _: original(0.025)):
                with self.assertRaisesRegex(PageReadError, "接口响应超时"):
                    await page._search({"pageNumber": 1})
        finally:
            noise_task.cancel()
            await asyncio.gather(noise_task, return_exceptions=True)
        self.assertIn("第 1 页", stages[-1])

    async def test_slow_control_call_has_one_preparation_deadline(self):
        page = OrdersPage(SimpleNamespace())

        async def slow(_):
            await asyncio.sleep(10)

        page.evaluate = slow
        original = asyncio.timeout
        with patch("backend.orders.asyncio.timeout", side_effect=lambda _: original(0.02)):
            with self.assertRaisesRegex(PageReadError, "控件准备超时"):
                await page._search({"pageNumber": 1})

    async def test_package_attention_uses_isolated_page_not_main_cdp(self):
        from backend.business_reader import collect as read

        session = SimpleNamespace(
            shop={"platform": "pdd"}, evaluate=AsyncMock(side_effect=AssertionError("main page must not be used"))
        )
        page = SimpleNamespace(open=AsyncMock())
        borrowed = []

        @asynccontextmanager
        async def borrow(s, kind, factory):
            borrowed.append(kind)
            yield page

        with (
            patch("backend.request_reuse.lookup", return_value=None),
            patch("backend.read_page_pool.borrow", borrow),
            patch("backend.operations.collect_operations", AsyncMock(return_value={"rows": []})),
        ):
            await read(session, "packages", {"_attention": True})
        self.assertEqual(borrowed, ["packages"])
        session.evaluate.assert_not_awaited()
        page.open.assert_awaited_once_with("packages")

    async def test_package_stage_is_recorded_before_first_response(self):
        from backend.operations import paginate

        stages = []
        page = SimpleNamespace(read_progress=lambda n, t, s: stages.append(s))
        with patch("backend.operations.read", AsyncMock(side_effect=TimeoutError())):
            with self.assertRaises(TimeoutError):
                await paginate(page, "packages", {"pageNumber": 1}, "pageNumber", "list")
        self.assertIn("包裹中心", stages[-1])
        self.assertIn("第 1 页", stages[-1])


if __name__ == "__main__":
    unittest.main()
