import asyncio
import json
import unittest
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.business_reader import ReadPage
from backend.orders import OrdersPage
from backend.goods_data import GoodsPage
from backend.promotions import PromotionPage
from backend.read_page_pool import borrow, close_idle
from backend.reader_document import DocumentGuard


class WorkTabTests(unittest.IsolatedAsyncioTestCase):
    def root(self):
        return SimpleNamespace(shop={"id": 1, "platform": "pdd"}, state="logged_in", port=9000, data_targets=set())

    def connect(self):
        async def attach(page, address):
            page.target_address = address
            page.ws = AsyncMock()
            page.call = AsyncMock()
            page.evaluate = AsyncMock(return_value=True)
            page.reader = asyncio.create_task(asyncio.Event().wait())

        return attach

    def create(self, created):
        attach = self.connect()

        async def enter(page):
            page.owner = AsyncMock()
            page.target = f"owned-{len(created)}"
            page.target_record = {"target_id": page.target}
            page.hidden_target = False
            page.session.data_targets.add(page.target)
            await attach(page, "ws://fixture/" + page.target)
            created.append(page)
            return page

        return enter

    async def test_cross_adapters_keep_one_target_and_replace_all_buffers(self):
        root, created = self.root(), []
        with (
            patch.object(ReadPage, "__aenter__", self.create(created)),
            patch.object(ReadPage, "connect_target", self.connect()),
            patch("backend.business_reader.close_reader_target", new=AsyncMock()) as close,
            patch("backend.reader_targets.forget"),
        ):
            previous = None
            for kind, factory in (
                ("overview", ReadPage),
                ("orders", OrdersPage),
                ("goodsdata", GoodsPage),
                ("ad_reports", PromotionPage),
            ):
                async with borrow(root, kind, factory) as page:
                    self.assertIs(type(page), factory)
                    self.assertEqual(page.target, "owned-0")
                    self.assertEqual(page.trade_responses, {})
                    if previous:
                        self.assertIsNone(previous.target)
                        self.assertGreater(page.response_generation, previous.response_generation)
                    page.trade_responses["old"] = {"sales": 999}
                    previous = page
            self.assertEqual(len(created), 1)
            self.assertEqual(root.data_targets, {"owned-0"})
            close.assert_not_awaited()
            await close_idle(root)
            close.assert_awaited_once()
            self.assertFalse(root.data_targets)

    async def test_real_parallel_demand_has_distinct_tabs_without_fixed_two_page_limit(self):
        root, created = self.root(), []
        with (
            patch.object(ReadPage, "__aenter__", self.create(created)),
            patch("backend.business_reader.close_reader_target", new=AsyncMock()),
            patch("backend.reader_targets.forget"),
        ):
            async with AsyncExitStack() as stack:
                pages = [await stack.enter_async_context(borrow(root, "orders", OrdersPage)) for _ in range(4)]
                self.assertEqual(len({page.target for page in pages}), 4)
                self.assertFalse(root._idle_read_pages)
            self.assertEqual(len(root._idle_read_pages), 4)
            async with borrow(root, "orders", OrdersPage) as reused:
                self.assertIn(reused, pages)
                self.assertEqual(len(created), 4)
            await close_idle(root)

    async def test_failed_rebind_closes_owned_target_and_does_not_cache_it(self):
        root, created = self.root(), []
        with (
            patch.object(ReadPage, "__aenter__", self.create(created)),
            patch("backend.business_reader.close_reader_target", new=AsyncMock()) as close,
            patch("backend.reader_targets.forget"),
        ):
            async with borrow(root, "orders", OrdersPage):
                pass
            # Failure after ownership transfer must dispose the old tab before
            # fallback creation; the active task still gets its own safe page.
            with patch.object(ReadPage, "connect_target", new=AsyncMock(side_effect=OSError("disconnected"))):
                async with borrow(root, "goodsdata", GoodsPage) as page:
                    self.assertEqual(page.target, "owned-1")
            self.assertEqual(len(created), 2)
            close.assert_awaited_once()
            await close_idle(root)

    async def test_idle_expiry_matches_saved_browser_idle_without_touching_active_sibling(self):
        root, created = self.root(), []
        root.read_page_idle_seconds = 1
        with (patch.object(ReadPage, '__aenter__', self.create(created)),
              patch.object(ReadPage, 'connect_target', self.connect()),
              patch('backend.business_reader.close_reader_target', new=AsyncMock()),
              patch('backend.reader_targets.forget')):
            async with borrow(root, 'orders', OrdersPage):pass
            timer = next(iter(root._idle_read_pages.values()))[3]
            self.assertLess(timer.when() - asyncio.get_running_loop().time(), 1.1)
            await close_idle(root)

    async def test_stop_during_rebind_prevents_new_reader_becoming_active(self):
        root, created = self.root(), []
        started, proceed = asyncio.Event(), asyncio.Event()
        attach = self.connect()

        async def delayed(page, address):
            started.set()
            await proceed.wait()
            await attach(page, address)

        with (
            patch.object(ReadPage, "__aenter__", self.create(created)),
            patch.object(ReadPage, "connect_target", delayed),
            patch("backend.business_reader.close_reader_target", new=AsyncMock()) as close,
            patch("backend.reader_targets.forget"),
        ):
            async with borrow(root, "orders", OrdersPage):
                pass

            async def run():
                async with borrow(root, "goodsdata", GoodsPage):
                    self.fail("stopped browser cannot be used")

            task = asyncio.create_task(run())
            await started.wait()
            await close_idle(root)
            proceed.set()
            with self.assertRaisesRegex(ValueError, "浏览器状态已变化"):
                await task
            self.assertFalse(root._idle_read_pages)
            close.assert_awaited_once()


class DocumentTests(unittest.TestCase):
    def frame(self, loader, url="https://mms.pinduoduo.com/orders/list"):
        return {"method": "Page.frameNavigated", "params": {"frame": {"id": "main", "loaderId": loader, "url": url}}}

    def request(self, loader, rid):
        return {
            "method": "Network.requestWillBeSent",
            "params": {"frameId": "main", "loaderId": loader, "requestId": rid},
        }

    def test_old_documents_and_old_request_completions_are_rejected(self):
        guard = DocumentGuard()
        guard.accepts(self.frame("old"))
        self.assertFalse(guard.accepts(self.request("old", "before-navigation")))
        guard.navigate("https://mms.pinduoduo.com/orders/list")
        guard.accepts(self.frame("new"))
        self.assertFalse(guard.accepts(self.request("old", "old-request")))
        self.assertTrue(guard.accepts(self.request("new", "new-request")))
        self.assertFalse(guard.accepts({"method": "Network.loadingFinished", "params": {"requestId": "old-request"}}))
        self.assertTrue(guard.accepts({"method": "Network.loadingFinished", "params": {"requestId": "new-request"}}))
        guard.navigate("https://yingxiao.pinduoduo.com/goods/report/promotion/overView")
        self.assertFalse(guard.accepts(self.request("new", "late")))
        guard.accepts(self.frame("login", "https://mms.pinduoduo.com/login"))
        self.assertFalse(guard.accepts(self.request("login", "unexpected")))

    def test_adapter_rebind_starts_with_no_old_scope_or_private_headers(self):
        for factory in (ReadPage, OrdersPage, GoodsPage, PromotionPage):
            page = factory(SimpleNamespace())
            self.assertFalse(page.finance_headers)
            self.assertFalse(page.trade_responses)
        order = OrdersPage(SimpleNamespace())
        order.order_requests["stale"] = {"pageNumber": 1}
        order.order_responses.put_nowait(({"pageNumber": 1}, {"rows": ["old"]}))
        order.reset_buffers()
        self.assertFalse(order.order_requests)
        self.assertTrue(order.order_responses.empty())


class ResponseIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cross_module_open_waits_for_committed_document_before_queries(self):
        from backend.business_reader import PAGES
        from backend.promotions import URLS

        for factory, kind in ((ReadPage, "overview"), (OrdersPage, "orders"), (PromotionPage, "ad_reports")):
            page = factory(SimpleNamespace())
            page.document_guard = DocumentGuard()
            expected = URLS[kind] if factory is PromotionPage else PAGES[kind]
            checks = []

            async def call(method, params=None):
                if method == "Page.navigate":
                    page.document_guard.navigate(params["url"])
                    # The ACK arrives with the old page's header/date controls
                    # still visible. There is deliberately no frame commit yet.
                    page.page_changed.set()
                return {}

            async def evaluate(script):
                if script == "performance.timeOrigin":
                    return 123
                checks.append(script)
                if len(checks) == 2:
                    page.document_guard.accepts(
                        {
                            "method": "Page.frameNavigated",
                            "params": {"frame": {"id": "main", "loaderId": "new-document", "url": expected}},
                        }
                    )
                page.page_changed.set()
                return {"ready": True, "login": False, "origin": 456}

            page.call = AsyncMock(side_effect=call)
            page.evaluate = AsyncMock(side_effect=evaluate)
            page.select = AsyncMock(return_value=True)
            with patch("backend.promotions.asyncio.sleep", new=AsyncMock()):
                if factory is PromotionPage:
                    await page.open_promotion(kind, {"trade_range": "realtime"})
                    page.select.assert_awaited_once_with("今日")
                else:
                    await page.open(kind)
            self.assertEqual(len(checks), 2)
            self.assertIn("performance.timeOrigin!==123", checks[0])
            self.assertIn("location.pathname", checks[0])
            self.assertIn(expected.split(".com", 1)[1], checks[0])

    async def test_order_and_promotion_receivers_do_not_observe_previous_document(self):
        from backend.orders import ENDPOINT
        from backend.promotions import SUMMARY

        for factory, url, endpoint, body in (
            (
                OrdersPage,
                "https://mms.pinduoduo.com/orders/list",
                "https://mms.pinduoduo.com" + ENDPOINT,
                {"pageSize": 20},
            ),
            (
                PromotionPage,
                "https://yingxiao.pinduoduo.com/goods/report/promotion/overView",
                "https://yingxiao.pinduoduo.com" + SUMMARY,
                {"startDate": "2026-10-29", "endDate": "2026-10-29"},
            ),
        ):
            page = factory(SimpleNamespace())
            page.document_guard = DocumentGuard()
            page.document_guard.navigate(url)
            events = [
                {
                    "method": "Page.frameNavigated",
                    "params": {"frame": {"id": "main", "loaderId": "current", "url": url}},
                },
                *[
                    {
                        "method": "Network.requestWillBeSent",
                        "params": {
                            "frameId": "main",
                            "loaderId": loader,
                            "requestId": loader,
                            "request": {"url": endpoint, "postData": json.dumps(body)},
                        },
                    }
                    for loader in ("old", "current")
                ],
                *[
                    {"method": "Network.loadingFinished", "params": {"requestId": loader}}
                    for loader in ("old", "current")
                ],
            ]

            class Socket:
                async def __aiter__(self):
                    for event in events:
                        yield json.dumps(event)
                        await asyncio.sleep(0)

            page.ws = Socket()
            page.call = AsyncMock(return_value={"body": '{"success":true,"result":{"fresh":true}}'})
            await page.receive()
            await asyncio.gather(*page.body_tasks)
            page.call.assert_awaited_once_with("Network.getResponseBody", {"requestId": "current"})
            self.assertEqual(page.order_responses.qsize() if factory is OrdersPage else len(page.events), 1)
