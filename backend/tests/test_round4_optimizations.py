"""Round 4: tab isolation, request pacing/cooldown, promotion date diagnosis,
status summary cache and per-shop upload wake-ups."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from backend import request_reuse, status_cache
from backend.read_page_pool import borrow, release_idle


class Reader:
    def __init__(self, session):
        self.session = session
        self.pending = {}
        self.body_tasks = set()
        self.evaluate = AsyncMock(return_value=True)
        self.call = AsyncMock()
        self.close = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


def root():
    return SimpleNamespace(shop={"id": 1, "platform": "pdd"}, state="logged_in", port=99)


class TabIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_page_creation_does_not_invalidate_other_tasks_tabs(self):
        shop = root()
        async with borrow(shop, "orders", Reader) as active:
            epoch = getattr(shop, "_read_page_pool_epoch", 0)
            # An exclusive direct reader (export, tax, logs) opening its page.
            await release_idle(shop)
            self.assertEqual(getattr(shop, "_read_page_pool_epoch", 0), epoch)
        # The active task's tab went back to the pool instead of being closed.
        active.close.assert_not_awaited()
        self.assertEqual(len(shop._idle_read_pages), 1)
        async with borrow(shop, "orders", Reader) as again:
            self.assertIs(again, active)

    async def test_release_idle_closes_only_idle_tabs(self):
        shop = root()
        idle = Reader(shop)
        async with borrow(shop, "tickets", Reader) as active:
            shop._idle_read_pages[id(idle)] = (idle, time.monotonic(), None, Mock())
            await release_idle(shop)
            idle.close.assert_awaited_once()
        active.close.assert_not_awaited()


class SoftSwitchTests(unittest.IsolatedAsyncioTestCase):
    def page(self, logged_in=True):
        from backend.business_reader import ReadPage

        page = ReadPage(SimpleNamespace(port=1, data_targets=set()))
        page.evaluate = AsyncMock(return_value=logged_in)
        page.call = AsyncMock()
        return page

    async def test_verified_in_page_module_skips_full_load(self):
        page = self.page()
        page.soft_switch = True
        await page.open("tickets")
        self.assertTrue(page.soft_opened)
        page.call.assert_not_awaited()  # no Page.navigate

    async def test_other_modules_and_logged_out_tabs_load_normally(self):
        for kind, logged_in in (("transactions", True), ("tickets", False)):
            page = self.page(logged_in)
            page.soft_switch = True
            page.page_changed.set()
            checks = [logged_in] if kind == "tickets" else []
            with patch.object(page, "evaluate", AsyncMock(side_effect=checks + [{"ready": True}] * 40)):
                await page.open(kind)
            self.assertFalse(page.soft_opened)
            self.assertIn("Page.navigate", [c.args[0] for c in page.call.await_args_list])

    async def test_failed_in_page_read_reloads_once(self):
        from backend.business_reader import soft_retry

        page = self.page()
        page.soft_opened = True
        page.open = AsyncMock()
        read = AsyncMock(side_effect=[ValueError("读取失败"), {"rows": []}])
        self.assertEqual(await soft_retry(page, "tickets", read), {"rows": []})
        page.open.assert_awaited_once_with("tickets")
        # Rate limits are never retried by reloading.
        page.soft_opened = True
        page.open.reset_mock()
        with self.assertRaises(ValueError):
            await soft_retry(page, "tickets", AsyncMock(side_effect=ValueError("请求频繁")))
        page.open.assert_not_awaited()

    async def test_soft_opened_tab_returns_to_pool_under_its_document_kind(self):
        shop = root()

        class Rebinding(Reader):
            async def rebind(self, session, factory):
                return self

        async with borrow(shop, "orders", Rebinding):
            pass
        async with borrow(shop, "tickets", Rebinding) as page:
            self.assertTrue(page.soft_switch)
            page.soft_opened = True
        (entry,) = shop._idle_read_pages.values()
        self.assertEqual(entry[0]._pool_kind, "orders")


class PacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_answer_starts_cooldown_without_more_requests(self):
        shop = root()
        fetch = AsyncMock(return_value={"success": False, "errorMsg": "请求频繁，请稍后再试"})
        with patch.dict("os.environ", {"ECMP_READ_MIN_INTERVAL": "0"}):
            await request_reuse.read(shop, "/api/list", {"page": 1}, fetch)
            with self.assertRaisesRegex(ValueError, "请求频繁"):
                await request_reuse.read(shop, "/api/list", {"page": 2}, fetch)
            # Other endpoints of the shop pause briefly as well.
            with self.assertRaisesRegex(ValueError, "冷却"):
                await request_reuse.read(shop, "/api/other", {"page": 1}, fetch)
        self.assertEqual(fetch.await_count, 1)
        from backend.collection_errors import classify

        self.assertEqual(classify(ValueError("平台提示请求频繁，接口冷却中"))[0], "rate_limit")

    async def test_rate_limit_exception_also_cools_down(self):
        shop = root()
        fetch = AsyncMock(side_effect=ValueError("操作太过频繁"))
        with patch.dict("os.environ", {"ECMP_READ_MIN_INTERVAL": "0"}):
            with self.assertRaises(ValueError):
                await request_reuse.read(shop, "/api/list", {"page": 1}, fetch)
            self.assertGreater(request_reuse.cooling(shop, "/api/list"), 200)

    async def test_requests_are_spaced_and_identical_requests_merged(self):
        shop = root()
        stamps = []

        async def fetch():
            stamps.append(time.monotonic())
            await asyncio.sleep(0.01)
            return {"success": True, "n": len(stamps)}

        with patch.dict("os.environ", {"ECMP_READ_MIN_INTERVAL": "0.1"}):
            same = await asyncio.gather(*(request_reuse.read(shop, "/mercury/mms/afterSales/queryList", {"p": 1}, fetch) for _ in range(3)))
            await request_reuse.read(shop, "/mercury/mms/afterSales/queryList", {"p": 2}, fetch)
        self.assertEqual(len(stamps), 2)  # three identical requests shared one fetch
        self.assertEqual({r["n"] for r in same}, {1})
        self.assertGreaterEqual(stamps[1] - stamps[0], 0.09)


class PromotionWaitTests(unittest.IsolatedAsyncioTestCase):
    def page(self):
        from backend.promotions import PromotionPage

        page = PromotionPage(SimpleNamespace(port=1, data_targets=set()))
        page.evaluate = AsyncMock(return_value={"login": False, "found": True, "selected": True})
        page.select = AsyncMock(return_value=True)
        return page

    async def test_query_never_fired_is_reported_as_such(self):
        page = self.page()
        options = {"trade_range": "week", "date_from": "2026-10-23", "date_to": "2026-10-29"}
        with self.assertRaisesRegex(Exception, "页面未触发查询"):
            await page.wait("/x", options, timeout=0.2, probe=0.05)

    async def test_login_loss_is_detected_early(self):
        page = self.page()
        page.evaluate.return_value = {"login": True}
        options = {"trade_range": "week", "date_from": "2026-10-23", "date_to": "2026-10-29"}
        started = time.monotonic()
        with self.assertRaisesRegex(Exception, "重新登录"):
            await page.wait("/x", options, timeout=5, probe=0.05)
        self.assertLess(time.monotonic() - started, 1)

    async def test_wrong_request_dates_fail_fast_with_both_ranges(self):
        page = self.page()
        options = {"trade_range": "week", "date_from": "2026-10-23", "date_to": "2026-10-29"}
        page.note_request("/x", {"beginDate": "2026-10-29", "endDate": "2026-10-29"})
        with self.assertRaisesRegex(Exception, "2026-10-29 至 2026-10-29 与指定范围"):
            await page.wait("/x", options, timeout=5, probe=0.05)

    async def test_unselected_range_is_clicked_again(self):
        page = self.page()
        page.evaluate.return_value = {"login": False, "found": True, "selected": False}
        options = {"trade_range": "week", "date_from": "2026-10-23", "date_to": "2026-10-29"}
        with self.assertRaises(Exception):
            await page.wait("/x", options, timeout=0.2, probe=0.05)
        page.select.assert_awaited_once()

    async def test_slow_response_is_distinguished(self):
        page = self.page()
        options = {"trade_range": "week", "date_from": "2026-10-23", "date_to": "2026-10-29"}
        page.ad_requests["r1"] = ("/x", {"beginDate": "2026-10-23", "endDate": "2026-10-29"}, page.response_generation)
        with self.assertRaisesRegex(Exception, "响应较慢"):
            await page.wait("/x", options, timeout=0.2, probe=0.05)

    async def test_matching_response_is_returned(self):
        page = self.page()
        options = {"trade_range": "week", "date_from": "2026-10-23", "date_to": "2026-10-29"}
        page.events.append(
            ("/x", {"beginDate": "2026-10-23", "endDate": "2026-10-29"}, {"success": True, "result": {"a": 1}})
        )
        data, _ = await page.wait("/x", options, timeout=1)
        self.assertEqual(data, {"a": 1})


class StatusCacheTests(unittest.TestCase):
    def test_loads_once_until_touched_or_expired(self):
        store = SimpleNamespace(directory="/tmp/x-status")
        load = Mock(side_effect=lambda: object())
        first = status_cache.get(store, 1, load, now=100)
        self.assertIs(status_cache.get(store, 1, load, now=101), first)
        self.assertEqual(load.call_count, 1)
        status_cache.touch(store, 1)
        self.assertIsNot(status_cache.get(store, 1, load, now=102), first)
        self.assertEqual(load.call_count, 2)
        status_cache.get(store, 1, load, now=102 + status_cache.TTL + 1)
        self.assertEqual(load.call_count, 3)
        status_cache.get(store, 2, load, now=200)  # other shops are independent
        self.assertEqual(load.call_count, 4)

    def test_write_during_load_keeps_entry_stale(self):
        store = SimpleNamespace(directory="/tmp/y-status")

        def load():
            status_cache.touch(store, 1)
            return "old"

        status_cache.get(store, 1, load, now=1)
        self.assertEqual(status_cache.get(store, 1, lambda: "new", now=2), "new")


class UploadWakeTests(unittest.IsolatedAsyncioTestCase):
    def test_fair_round_robin_between_shops(self):
        from backend.center.delivery_outbox import fair

        rows = [(1, "a"), (1, "b"), (1, "c"), (2, "d"), (3, "e"), (2, "f")]
        self.assertEqual([r[1] for r in fair(rows)], ["a", "d", "e", "b", "f", "c"])

    def test_hint_names_only_the_publishing_shop(self):
        from backend.center.delivery_notify import deliver
        from backend.center.shop_delivery import ShopDeliveryGroup

        with tempfile.TemporaryDirectory() as folder:
            group = ShopDeliveryGroup(folder, "business", ["c", "n"])
            plain = asyncio.Event()
            deliver([group, plain], b"publish:7")
            self.assertEqual(group.woken, {"7"})
            self.assertFalse(group.wake_all)
            self.assertTrue(plain.is_set() and group.wake.is_set())
            deliver([group], b"publish")
            self.assertTrue(group.wake_all)
            deliver([group], b"garbage")

    def test_discovery_reuses_result_until_files_change(self):
        import json
        import sqlite3
        from backend.center.shop_delivery import ShopDeliveryGroup

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            from contextlib import closing

            with closing(sqlite3.connect(directory / "shops.sqlite3")) as db, db:
                db.execute("CREATE TABLE shops(id INTEGER)")
                db.execute("INSERT INTO shops VALUES(1)")
            (directory / "shop-storage.json").write_text(
                json.dumps({"shops": {"1": {"state": "active", "path": "s1/shop.sqlite3"}}})
            )
            group = ShopDeliveryGroup(folder, "business", ["c", "n"])
            self.assertTrue(group.discover())
            self.assertFalse(group.discover())
            self.assertEqual(set(group.children), {"1"})
            (directory / "shop-storage.json").write_text(json.dumps({"shops": {}}))
            self.assertTrue(group.discover())
            self.assertEqual(group.children, {})

    def test_idle_wait_backs_off(self):
        from backend.center.replication_worker import idle_wait

        self.assertEqual(idle_wait(0), 0.2)
        self.assertEqual(idle_wait(1), 5)
        self.assertEqual(idle_wait(6), 10)
        self.assertEqual(idle_wait(30), 20)


if __name__ == "__main__":
    unittest.main()
