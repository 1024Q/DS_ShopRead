import asyncio, tempfile, time, unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
from fastapi import FastAPI
from backend import collection, collection_plan, incremental_collection as incremental
from backend.business import BusinessService, options_for, create_router
from backend.browser import Session
from backend.store import Store
from backend.scheduler import Scheduler
from backend.collection_queue import select_jobs, waiting_reason
from backend.business_reader import BEIJING
from backend.read_errors import LoginPending
from backend.tests.collection_fixture import assign


class OptimizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("test", "pw")
        self.shop = self.store.list()[0]
        self.item = Session(self.shop, self.store)
        self.item.state = "logged_in"
        self.item.window = AsyncMock()
        self.item.visible = False
        self.service = BusinessService(self.store, lambda _: self.item)
        self.scheduler = Scheduler(self.service)
        from backend.tests.collection_fixture import assign

        collection.save_settings(self.store, {"settings_patch": {"keepalive_enabled": False}})
        assign(self.store, {"reviews@base": "custom"}, reset=True)

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    async def drain(self):
        for _ in range(200):
            self.service.due_plans.dispatch()
            if self.service.jobs:
                await asyncio.wait_for(asyncio.gather(*list(self.service.jobs.values())), 5)
            elif not any(self.service.due_plans.pending(k) for k in self.service.due_plans.entries):
                return
            else:
                await asyncio.sleep(.01)
        self.fail('Due intents did not finish')

    async def test_report_history_is_collected_without_opening_page_and_reused(self):
        config = assign(
            self.store, {"reviews@base": "off", "ad_reports@base": "realtime", "ad_reports@month": "history_rolling"}
        )
        with patch("backend.business.collect", new=AsyncMock(return_value={"rows": [], "complete": True})) as reader:
            await self.scheduler.tick_shop(self.shop, config)
            ranges = {
                options["trade_range"] for _, _, kind, options, _ in self.scheduler.candidates if kind == "ad_reports"
            }
            self.assertEqual(ranges, {"realtime", "yesterday", "week", "month"})
            from backend.promotions import options_for as ad_options

            for span in ranges:
                opts = ad_options(span)
                key = self.service.key("ad_reports", opts, self.shop)
                self.store.write_cache(self.sid, key, {"rows": [], "complete": True})
                collection.register(self.store, self.sid, key, "ad_reports", opts, self.store.cache_info(self.sid, key))
                collection.finish(self.store, self.sid, key)
            self.scheduler.candidates = []
            await self.scheduler.tick_shop(self.shop, config)
            self.assertFalse([c for c in self.scheduler.candidates if c[2] == "ad_reports"])

    async def test_filters_share_collection_and_keep_database_filtering(self):
        today = datetime.now(BEIJING).date().isoformat()
        rows = [
            {"id": "one", "created_at": today, "stars": 1, "replied": False},
            {"id": "five", "created_at": today, "stars": 5, "replied": True},
        ]
        with patch("backend.business.collect", new=AsyncMock(return_value={"rows": rows, "complete": True})) as reader:
            self.service.request_sync("reviews", options_for("reviews", stars="1"))
            self.service.request_sync("reviews", options_for("reviews", stars="1,2", reply="unreplied"))
            self.assertEqual(len(self.service.jobs), 1)
            await self.drain()
            reader.assert_awaited_once()
            self.assertEqual(reader.call_args.args[2]["stars"], [])
            app = FastAPI()
            app.include_router(create_router(self.service, lambda: None))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                result = (await client.get("/api/data/reviews?stars=5&reply=replied")).json()
            self.assertEqual([r["id"] for r in result["rows"]], ["five"])
            self.service.request_sync("reviews", options_for("reviews", stars="5"))
            self.assertFalse(self.service.jobs)

    async def test_one_shot_history_exits_schedule_and_cache_remains(self):
        day = (datetime.now(BEIJING).date() - timedelta(days=10)).isoformat()
        opts = options_for("reviews", day, day)
        key = self.service.key("reviews", opts, self.shop)
        with patch("backend.business.collect", new=AsyncMock(return_value={"rows": [], "complete": True})) as reader:
            self.service.request_sync("reviews", opts)
            await self.drain()
            with self.store.connect() as db:
                db.execute("UPDATE collection_tasks SET last_success=last_success-1000")
                db.execute("UPDATE business_cache SET updated=updated-1000")
            await self.scheduler.tick()
            await self.scheduler.refill()
            await self.drain()
            self.assertEqual(sum(c.args[1] == "reviews" for c in reader.call_args_list), 2)
        self.assertEqual(collection.state(self.store, self.sid, key)["schedule_state"], "archived")
        self.assertIsNotNone(self.store.read_cache(self.sid, key))

    async def test_partial_broad_cache_uses_narrow_one_shot_instead_of_claiming_coverage(self):
        options = options_for("reviews")
        broad = self.service.key("reviews", options, self.shop)
        self.store.write_cache(self.sid, broad, {"rows": [], "complete": False, "source_total": 5000})
        selected = options_for("reviews", stars="1")
        resolved = self.service.query_options("reviews", selected, self.shop)
        self.assertTrue(resolved["_filtered_read"])
        self.assertEqual(resolved["stars"], [1])
        with patch("backend.business.collect", new=AsyncMock(return_value={"rows": [], "complete": True})) as reader:
            self.service.request_sync("reviews", selected)
            await self.drain()
            self.assertEqual(reader.call_args.args[2]["stars"], [1])
            self.service.request_sync("reviews", selected)
            self.assertFalse(self.service.jobs)

    async def test_expired_realtime_demand_is_not_retried(self):
        assign(self.store, {"transactions@base": "realtime"})
        old = {"trade_range": "realtime", "day": "2020-01-01"}
        key = self.service.key("transactions", old, self.shop)
        collection.register(self.store, self.sid, key, "transactions", old)
        collection.finish(self.store, self.sid, key, error="bad")
        with self.store.connect() as db:
            db.execute("UPDATE collection_tasks SET retry_at=0")
        with patch("backend.business.collect", new=AsyncMock(return_value={"rows": [], "complete": True})) as reader:
            await self.scheduler.tick()
            await self.scheduler.refill()
            await self.drain()
            self.assertTrue(all(c.args[2].get("day") != "2020-01-01" for c in reader.call_args_list))
        self.assertEqual(collection.state(self.store, self.sid, key)["schedule_state"], "archived")

    async def test_removed_stagger_setting_cannot_delay_current_tasks(self):
        before = collection.settings(self.store)
        with self.assertRaises(ValueError):
            collection.save_settings(self.store, {"stagger_seconds": 60})
        self.assertEqual(collection.settings(self.store), before)
        with patch("backend.business.collect", new=AsyncMock(return_value={"rows": [], "complete": True})) as reader:
            self.service.request_sync("reviews", options_for("reviews"))
            await self.drain()
            reader.assert_awaited_once()


class AdmissionOptimization(unittest.TestCase):
    def test_home_priority_and_retry_limit(self):
        def job(kind, **kw):
            return {"kind": kind, "phase": "queued", "queued_at": 999, "scope": {}, **kw}

        progress = {
            (1, "reviews"): job("reviews"),
            (1, "overview"): job("overview"),
            (2, "products"): job("products", failures=8),
            (3, "products"): job("products", failures=5),
            (4, "transactions"): job("transactions"),
        }
        cfg = {
            **collection.DEFAULTS,
            "max_concurrent": 4,
            "realtime_concurrent": 3,
            "daily_concurrent": 1,
            "shop_concurrent": 1,
        }
        result = select_jobs(progress, cfg, now=1000)
        self.assertIn((1, "overview"), result)
        self.assertNotIn((1, "reviews"), result)
        self.assertEqual(sum(k[1] == "products" for k in result), 1)
        progress[(2, "products")]["phase"] = "running"
        self.assertEqual(waiting_reason((3, "products"), progress[(3, "products")], progress, cfg), "等待异常重试名额")

    def test_long_wait_has_bounded_precedence(self):
        progress = {
            (1, "reviews"): {"kind": "reviews", "phase": "queued", "queued_at": 100, "scope": {}},
            (1, "overview"): {"kind": "overview", "phase": "queued", "queued_at": 999, "scope": {}},
        }
        self.assertEqual(select_jobs(progress, collection.DEFAULTS, now=1000)[0], (1, "reviews"))

    def test_backoff_frequencies_and_platform_boundaries(self):
        self.assertEqual(
            [collection.retry_delay(i) for i in (1, 2, 3, 4, 5, 6, 10)], [60, 120, 300, 900, 1800, 3600, 3600]
        )
        self.assertEqual(collection.interval_seconds("overview", collection.DEFAULTS), 60)
        self.assertEqual(collection.interval_seconds("goodsdata", collection.DEFAULTS), 60)
        opts = options_for("reviews", stars="1")
        self.assertEqual(collection_plan.canonical("reviews", opts, "taobao"), opts)


class IncrementalTests(unittest.IsolatedAsyncioTestCase):
    def setup_data(self):
        self.session = SimpleNamespace(shop={"platform": "pdd"})
        self.options = options_for("packages")
        self.config = dict(collection.DEFAULTS)
        self.previous = {
            "rows": [
                {"id": "done", "status": "已签收"},
                {"id": "open", "status": "发货未揽件", "order_number": "o", "tracking_number": "t"},
            ],
            "complete": True,
            "_sync": {"full_at": time.time()},
        }

    async def test_full_daily_boundary_manual_and_unsupported(self):
        self.setup_data()
        reader = AsyncMock(return_value={"rows": [], "complete": True})
        with patch.object(
            incremental,
            "package_delta",
            new=AsyncMock(return_value={"rows": [], "_sync": {"collection_mode": "incremental"}}),
        ) as delta:
            await incremental.read(
                self.session, "packages", self.options, {"payload": self.previous}, self.config, False, reader
            )
            delta.assert_awaited_once()
            reader.assert_not_awaited()
            await incremental.read(
                self.session, "packages", self.options, {"payload": self.previous}, self.config, True, reader
            )
            self.previous["_sync"]["full_at"] = incremental.boundary(self.config) - 1
            await incremental.read(
                self.session, "packages", self.options, {"payload": self.previous}, self.config, False, reader
            )
            await incremental.read(
                self.session, "reviews", self.options, {"payload": self.previous}, self.config, False, reader
            )
            self.assertEqual(reader.await_count, 3)

    async def test_delta_preserves_history_and_reconciles_disappeared_open_parcel(self):
        self.setup_data()
        page = SimpleNamespace(open=AsyncMock())

        class Context:
            async def __aenter__(self):
                return page

            async def __aexit__(self, *args):
                pass

        async def part(page, kind, body, *args):
            rows = (
                [{"id": "open", "status": "已签收", "order_number": "o", "tracking_number": "t"}]
                if body["orderSn"] == "o"
                else []
            )
            if body["shippingTimeStart"] > self.options["start"]:
                rows.append({"id": "new", "status": "已签收"})
            return {"rows": rows, "complete": True, "source_loaded": len(rows)}

        with (
            patch.object(incremental, "ReadPage", return_value=Context()),
            patch.object(incremental, "paginate", side_effect=part),
        ):
            data = await incremental.package_delta(self.session, self.options, self.previous)
        self.assertEqual({r["id"] for r in data["rows"]}, {"done", "open", "new"})
        self.assertEqual(next(r for r in data["rows"] if r["id"] == "open")["status"], "已签收")

    async def test_incomplete_delta_falls_back_and_login_is_not_swallowed(self):
        self.setup_data()
        reader = AsyncMock(return_value={"rows": [], "complete": True})
        with patch.object(incremental, "package_delta", new=AsyncMock(side_effect=ValueError("partial"))):
            data = await incremental.read(
                self.session, "packages", self.options, {"payload": self.previous}, self.config, False, reader
            )
        self.assertEqual(data["_sync"]["collection_mode"], "full")
        reader.reset_mock()
        with patch.object(incremental, "package_delta", new=AsyncMock(side_effect=LoginPending("验证码"))):
            with self.assertRaises(LoginPending):
                await incremental.read(
                    self.session, "packages", self.options, {"payload": self.previous}, self.config, False, reader
                )
        reader.assert_not_awaited()

    def ticket_snapshot(self):
        self.options = options_for("tickets")
        base = self.options["start"]
        text = lambda offset: datetime.fromtimestamp(base + offset, BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        self.times = {"closed": base + 100, "moved": base + 200, "waiting": base + 300}
        self.previous = {
            "rows": [
                {"id": "closed", "status": "已完结", "created_at": text(100)},
                {"id": "moved", "status": "处理中", "created_at": text(200)},
                {"id": "waiting", "status": "待处理", "created_at": text(300)},
            ],
            "complete": True,
            "_sync": {"full_at": time.time(), "snapshot_started_at": base + 86400},
        }

    async def test_list_delta_reads_since_last_snapshot_and_reconciles_closed_records(self):
        self.ticket_snapshot()
        calls = []

        async def reader(options):
            calls.append(options)
            if options["status"] == "待处理":
                rows = [{"id": "waiting", "status": "待处理"}]
            elif options["status"]:
                rows = []
            elif options["start"] == self.times["moved"]:
                rows = [{"id": "moved", "status": "已完结"}]
            else:
                rows = [{"id": "new", "status": "待处理"}]
            return {"rows": rows, "complete": True, "source_loaded": len(rows)}

        data = await incremental.merge_delta(
            "tickets", self.options, self.previous, self.options["start"] + 86400 - incremental.OVERLAP, reader
        )
        self.assertEqual(
            {r["id"]: r["status"] for r in data["rows"]},
            {"closed": "已完结", "moved": "已完结", "waiting": "待处理", "new": "待处理"},
        )
        self.assertEqual(calls[0]["start"], self.options["start"] + 86400 - incremental.OVERLAP)
        self.assertEqual((calls[-1]["start"], calls[-1]["end"]), (self.times["moved"], self.times["moved"]))
        self.assertEqual(data["_sync"]["collection_mode"], "incremental")

        async def vanished(options):
            return {"rows": [], "complete": True, "source_loaded": 0}

        with self.assertRaises(ValueError):
            await incremental.merge_delta("tickets", self.options, self.previous, self.options["start"], vanished)

    async def test_list_delta_requires_watermark_and_unfiltered_scope(self):
        self.ticket_snapshot()
        self.session = SimpleNamespace(shop={"platform": "pdd"})
        config = dict(collection.DEFAULTS)
        reader = AsyncMock(return_value={"rows": [], "complete": True})
        with patch.object(incremental, "list_delta", new=AsyncMock(return_value={"rows": []})) as delta:
            await incremental.read(
                self.session, "tickets", self.options, {"payload": self.previous}, config, False, reader
            )
            delta.assert_awaited_once()
            self.assertEqual(delta.await_args.args[-1], self.options["start"] + 86400 - incremental.OVERLAP)
            await incremental.read(
                self.session,
                "tickets",
                {**self.options, "status": "处理中"},
                {"payload": self.previous},
                config,
                False,
                reader,
            )
            del self.previous["_sync"]["snapshot_started_at"]
            await incremental.read(
                self.session, "tickets", self.options, {"payload": self.previous}, config, False, reader
            )
        self.assertEqual(reader.await_count, 2)

    async def test_invoice_delta_dates_only_new_and_lookup_reads(self):
        self.ticket_snapshot()
        self.options = options_for("invoices")
        calls = []

        async def reader(options):
            calls.append(options)
            return {"rows": [], "complete": True, "source_loaded": 0}

        await incremental.merge_delta(
            "invoices",
            self.options,
            {"rows": [], "complete": True, "_sync": {"full_at": 1}},
            self.options["start"],
            reader,
        )
        self.assertEqual([(c["status"], bool(c.get("_exact_dates"))) for c in calls], [("", True)])
