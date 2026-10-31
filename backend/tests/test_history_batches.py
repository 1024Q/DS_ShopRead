import asyncio
import copy
import json
import tempfile
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend import collection, collection_checkpoint as cp, goods_data, promotions
from backend.read_budget import current as read_budget
from backend.read_errors import PageReadError
from backend.store import Store
from backend.business import BusinessService
from backend.browser import Session
from backend.tests.test_goods_data import raw as goods_raw
from backend.tests.test_promotions import raw as promotion_raw


class HistoryBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("history-test", "pw")
        self.key = "ad_products:history"
        self.old = {"rows": [{"id": "previous-complete"}], "complete": True}
        self.store.write_cache(self.sid, self.key, self.old)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def promotion_page(self, identifiers, size=1):
        page = SimpleNamespace(position=1, calls=[], moves=[])

        async def wait(path, options, number, mode):
            self.assertEqual(number, page.position)
            page.calls.append(number)
            selected = identifiers[(number - 1) * size : number * size]
            return {"adInfos": [promotion_raw(i) for i in selected], "totalAdNum": len(identifiers)}, {}

        page.wait = wait
        return page

    async def move(self, page, target, previous, family):
        self.assertEqual(page.position, previous)
        page.moves.append((previous, target))
        page.position = target

    async def test_promotions_resume_by_page_preserve_old_snapshot_and_all_rows(self):
        rounds = []
        with (
            patch("backend.native_pagination.move", self.move),
            patch("backend.collection_checkpoint.asyncio.sleep", AsyncMock()),
        ):
            for attempt in range(6):
                page = self.promotion_page(list(range(1, 8)))
                rounds.append(page)
                with cp.context(self.store, self.sid, self.key, {"batch_pages": 3, "batch_seconds": 60}):
                    try:
                        rows, total, done = await promotions.read_rows(
                            page, "ad_products", promotions.options_for("month"), 1
                        )
                    except cp.YieldCollection:
                        self.assertEqual(self.store.read_cache(self.sid, self.key)["payload"], self.old)
                        continue
                break
            else:
                self.fail("history did not finish")
        self.assertTrue(done)
        self.assertEqual(total, 7)
        self.assertEqual({r["id"] for r in rows}, {str(i) for i in range(1, 8)})
        self.assertEqual(rounds[0].calls, [1, 2, 3])
        self.assertEqual(rounds[1].calls[:3], [1, 3, 4])
        self.assertNotIn(2, rounds[1].calls)

    async def test_historical_list_no_longer_truncates_at_2000(self):
        with (
            patch("backend.native_pagination.move", self.move),
            patch("backend.collection_checkpoint.asyncio.sleep", AsyncMock()),
        ):
            for _ in range(4):
                page = self.promotion_page(list(range(1, 2006)), 1000)
                with cp.context(self.store, self.sid, self.key, {"batch_pages": 1, "batch_seconds": 60}):
                    try:
                        rows, total, done = await promotions.read_rows(
                            page, "ad_products", promotions.options_for("week"), 1
                        )
                    except cp.YieldCollection:
                        continue
                break
            else:
                self.fail("large history did not finish")
        self.assertEqual((len(rows), total, done), (2005, 2005, True))

    async def test_completed_managed_part_ids_are_not_mutated_in_checkpoint(self):
        with cp.context(self.store, self.sid, self.key, {}) as ctx:
            page = self.promotion_page([1])
            rows, _, _ = await promotions.read_rows(page, "ad_products", promotions.options_for("yesterday"), 3)
            rows[0]["id"] = "managed:" + rows[0]["id"]
            self.assertEqual(next(iter(ctx.data["parts"].values()))["rows"]["1"]["id"], "1")

    async def test_goods_resume_keeps_decoded_pages_and_initial_summary(self):
        opts = goods_data.options_for("week")
        opts["day"] = "2026-10-14"

        def batch(n, glyph):
            return {"totalNum": 4, "goodsDetailList": [{"goodsId": n, "goodsUv": glyph, "payOrdrAmt": glyph}]}

        results = []
        with (
            patch("backend.native_pagination.move", self.move),
            patch("backend.collection_checkpoint.asyncio.sleep", AsyncMock()),
        ):
            for turn, glyph in enumerate(("\ue001", "\ue002")):
                page = SimpleNamespace(
                    position=1,
                    moves=[],
                    detail_serial=0,
                    detail_scope={"startDate": "2026-10-07", "endDate": "2026-10-13", "pageSize": 50, "queryType": 1},
                    trade_responses={},
                    evaluate=AsyncMock(return_value="original-time"),
                )

                async def wait_detail(options, number, after):
                    self.assertEqual(page.position, number)
                    return batch(number, glyph)

                page.wait_detail = wait_detail
                raw = goods_raw()
                raw[goods_data.DETAIL] = batch(1, glyph)
                raw[goods_data.HISTORY]["payOrdrAmt"] = str(200 + turn)
                with (
                    cp.context(self.store, self.sid, self.key, {"batch_pages": 2, "batch_seconds": 60}),
                    patch("backend.goods_data.display_map", AsyncMock(return_value={ord(glyph): str(turn + 1)})),
                ):
                    try:
                        result = await goods_data.historical_rows(page, raw, opts)
                        results.append(result)
                    except cp.YieldCollection:
                        self.assertEqual(turn, 0)
        result = results[0]
        self.assertTrue(result["complete"])
        self.assertEqual(result["source_total"], 4)
        self.assertEqual(result["metrics"]["summary"]["sales"], 200)
        by_id = {row["id"]: row for row in result["rows"]}
        self.assertEqual(by_id["2"]["visitors"], 1)
        self.assertEqual(by_id["4"]["visitors"], 2)

    async def test_final_validation_yield_does_not_fetch_nonexistent_page(self):
        calls = []

        async def fetch(number):
            calls.append(number)
            self.assertLessEqual(number, 2)
            return {"rows": [{"id": str(number)}], "ids": [str(number)], "total": 2}

        budget = SimpleNamespace(seconds=10, remaining=lambda: 10 if len(calls) < 2 else 0.01, database=nullcontext)
        token = read_budget.set(budget)
        try:
            with cp.context(self.store, self.sid, self.key, {"batch_pages": 10, "batch_seconds": 60}):
                with self.assertRaises(cp.YieldCollection):
                    await cp.paginate("last-page", fetch, lambda *_: None)
        finally:
            read_budget.reset(token)
        self.assertEqual(calls, [1, 2])
        with cp.context(self.store, self.sid, self.key, {"batch_pages": 10, "batch_seconds": 60}):
            result = await cp.paginate("last-page", fetch, lambda *_: None)
        self.assertEqual(calls, [1, 2, 1, 2])
        self.assertTrue(result["complete"])

    async def test_native_paging_failure_retains_saved_pages(self):
        page = self.promotion_page([1, 2])
        with cp.context(self.store, self.sid, self.key, {"batch_pages": 1}):
            with self.assertRaises(cp.YieldCollection):
                await promotions.read_rows(page, "ad_products", promotions.options_for("month"), 1)
        with (
            cp.context(self.store, self.sid, self.key, {}) as ctx,
            patch("backend.native_pagination.move", AsyncMock(side_effect=PageReadError("pagination unavailable"))),
        ):
            with self.assertRaises(PageReadError):
                await promotions.read_rows(
                    self.promotion_page([1, 2]), "ad_products", promotions.options_for("month"), 1
                )
            self.assertEqual(next(iter(ctx.data["parts"].values()))["ids"], ["1"])
        self.assertEqual(self.store.read_cache(self.sid, self.key)["payload"], self.old)

    async def test_changed_history_head_restarts_summary_and_all_parts_together(self):
        opts = promotions.options_for("month")
        collection.register(self.store, self.sid, self.key, "ad_products", opts)
        with cp.context(self.store, self.sid, self.key, {"batch_pages": 1}) as ctx:
            ctx.data["promotion_summary"] = {"ad_spend": 10}
            with self.assertRaises(cp.YieldCollection):
                await promotions.read_rows(self.promotion_page([1, 2]), "ad_products", opts, 1)
        with cp.context(self.store, self.sid, self.key, {}) as ctx:
            with self.assertRaises(cp.YieldCollection):
                await promotions.read_rows(self.promotion_page([3, 1, 2]), "ad_products", opts, 1)
            self.assertNotIn("promotion_summary", ctx.data)
            self.assertEqual(ctx.data["parts"], {})
        self.assertEqual(self.store.read_cache(self.sid, self.key)["payload"], self.old)
        self.assertTrue(collection.state(self.store, self.sid, self.key)["continuation"])


class ScopedGoodsResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_ignores_old_operation_wrong_dates_and_wrong_page(self):
        page = goods_data.GoodsPage(SimpleNamespace())
        good = {"startDate": "2026-10-06", "endDate": "2026-10-12", "pageNum": 1, "pageSize": 50, "queryType": 1}
        body = lambda marker: {"success": True, "result": {"marker": marker}}
        page.detail_events = [
            (1, {**good, "queryType": 6}, body("before-click")),
            (2, good, body("native-ready-range")),
        ]
        opts = {"day": "2026-10-14", "trade_range": "week"}
        self.assertEqual(await page.wait_detail(opts, 1, after=1), {"marker": "native-ready-range"})
        self.assertEqual(page.detail_scope["endDate"], "2026-10-12")
        good = {**good, "pageNum": 4}
        page.detail_events = [
            (2, good, body("before-page-click")),
            (4, {**good, "pageNum": 2}, body("wrong-page")),
            (5, {**good, "endDate": "2026-10-13"}, body("wrong-range")),
            (3, good, body("matching")),
            (6, {**good, "pageSize": 10}, body("wrong-size")),
            (7, {**good, "queryType": 6}, body("wrong-type")),
        ]
        value = await page.wait_detail(opts, 4, after=2)
        self.assertEqual(value, {"marker": "matching"})

    async def test_request_observer_keeps_only_scope_fields(self):
        page = goods_data.GoodsPage(SimpleNamespace())
        message = {
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "one",
                "request": {
                    "url": "https://mms.pinduoduo.com/sydney/api/goodsDataShow/" + goods_data.DETAIL,
                    "postData": json.dumps(
                        {
                            "pageNum": 2,
                            "pageSize": 50,
                            "startDate": "2026-10-13",
                            "endDate": "2026-10-13",
                            "crawlerInfo": "not-persisted",
                        }
                    ),
                },
            },
        }
        page.observe(message)
        self.assertEqual(page.detail_requests, {})  # Live reads do not duplicate response capture.
        page.capture_detail = True
        page.observe(message)
        serial, scope = page.detail_requests["one"]
        self.assertEqual(serial, 1)
        self.assertNotIn("crawlerInfo", scope)
        self.assertEqual(scope["pageNum"], 2)


class HistoryAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("admission-test", "pw")
        self.shop = self.store.list()[0]
        self.item = Session(self.shop, self.store)
        self.item.state = "logged_in"
        self.item.window = AsyncMock()
        self.service = BusinessService(self.store, lambda _: self.item)
        from backend.tests.collection_fixture import assign

        assign(
            self.store, {"goodsdata@month": "history_rolling", "goodsdata@base": "realtime", "orders@today": "realtime"}
        )

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def test_all_continuations_receive_new_queue_turn(self):
        for kind, opts, expected in [
            ("goodsdata", goods_data.options_for("week"), True),
            ("goodsdata", goods_data.options_for("realtime"), True),
            ("orders", {"_order_lane": "recent"}, True),
        ]:
            key = kind + ":" + str(expected) + str(len(self.service.jobs))
            task = {"continuation": True, "failures": 0, "retry_at": 0, "last_success": 0}
            prepared = (opts, key, None, collection.settings(self.store), task)
            with patch.object(self.service, "sync", AsyncMock()):
                self.service.admit(self.shop, self.item, kind, prepared, True, False, False)
                meta = self.service.progress.get((self.sid, key)) or self.service.due_plans.entries[self.sid, key][1]
                self.assertEqual("queue_turn_at" in meta, expected)
                if expected:
                    self.assertEqual(meta["queue_turn_at"], meta["queued_at"])

    async def test_business_yield_preserves_complete_cache_and_sets_continuation(self):
        opts = goods_data.options_for("month")
        key = self.service.key("goodsdata", opts, self.shop)
        old = {"rows": [{"id": "previous"}], "complete": True}
        self.store.write_cache(self.sid, key, old)
        with self.store.connect() as db:
            db.execute("UPDATE business_cache SET updated=updated-60 WHERE shop_id=? AND cache_key=?", (self.sid, key))
        observed = []

        async def reader(*args):
            observed.append((cp.current.get().budget, cp.current.get().seconds))

            async def fetch(number):
                return {"rows": [{"id": str(number)}], "ids": [str(number)], "total": 9}

            return await cp.paginate("history", fetch, lambda *_: None)

        with (
            patch("backend.business.collect", reader),
            patch("backend.collection_checkpoint.asyncio.sleep", AsyncMock()),
        ):
            reply = self.service.request_sync("goodsdata", opts, force=True)
            self.assertTrue(self.service.jobs, reply)
            await asyncio.gather(*self.service.jobs.values())
        self.assertEqual(observed, [(3, 8)], self.service.errors)
        state = collection.state(self.store, self.sid, key)
        self.assertTrue(state["continuation"])
        self.assertEqual(state["failures"], 0)
        self.assertEqual(self.store.read_cache(self.sid, key)["payload"], old)
