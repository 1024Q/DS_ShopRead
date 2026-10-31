import asyncio
import copy
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend import collection, home_progress, promotions, summary_publication, transactions
from backend.business import BusinessService, scheduling_states
from backend.browser import Session
from backend.collection_checkpoint import Checkpoint
from backend.store import Store
from backend.tests.test_reader_summary_performance import promotion_summary, trade_core


class SummaryOnlyReaders(unittest.IsolatedAsyncioTestCase):
    async def test_trade_summary_returns_without_waiting_for_auxiliary_widgets(self):
        wait = AsyncMock(return_value=trade_core())
        published = AsyncMock()
        with (
            summary_publication.context(published, summary_only=True),
            patch("backend.transactions.wait_responses", wait),
            patch("backend.transactions.display_map", AsyncMock(return_value={})),
            patch("backend.transactions.source_time", AsyncMock(return_value=None)),
        ):
            result = await transactions.collect(SimpleNamespace(), {"trade_range": "realtime", "day": "2026-10-29"})
        wait.assert_awaited_once()
        published.assert_not_awaited()  # BusinessService commits the completed summary once.
        self.assertEqual(wait.await_args.args[1], transactions.CORE_ENDPOINTS)
        self.assertTrue(result["summary_complete"])
        self.assertTrue(result["details_on_demand"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["metrics"]["summary"]["sales"], 100)
        self.assertEqual(len(result["metrics"]["trend"]), 1)
        self.assertNotIn("annual", result["metrics"])

    async def test_ad_summary_keeps_all_metrics_without_reading_products(self):
        page = SimpleNamespace(open_promotion=AsyncMock(), wait=AsyncMock(return_value=(promotion_summary(), {})))

        @asynccontextmanager
        async def borrow(*args):
            yield page

        details = AsyncMock(side_effect=AssertionError("summary must not read product pages"))
        published = AsyncMock()
        with (
            summary_publication.context(published, summary_only=True),
            patch("backend.read_page_pool.borrow", borrow),
            patch("backend.promotions.read_rows", details),
        ):
            result = await promotions.collect(SimpleNamespace(), "ad_reports", promotions.options_for())
        details.assert_not_awaited()
        published.assert_not_awaited()
        self.assertTrue(result["details_on_demand"])
        self.assertEqual(result["metrics"]["total_spend"], 15.67)
        self.assertEqual(result["metrics"]["ad_spend"], 12.34)
        self.assertEqual(result["metrics"]["ad_revenue"], 100)
        self.assertTrue(set(promotions.FIELDS) <= set(result["metrics"]))

    def test_only_scheduled_pdd_realtime_uses_summary_policy(self):
        select = summary_publication.scheduled
        self.assertTrue(select("transactions", {"trade_range": "realtime"}))
        self.assertFalse(select("transactions", {"trade_range": "realtime"}, force=True))
        self.assertFalse(select("transactions", {"trade_range": "realtime"}, "jd"))
        self.assertFalse(select("ad_reports", {"trade_range": "yesterday"}))
        self.assertFalse(select("ad_products", {"trade_range": "realtime"}))


class SummaryOnlyPublication(unittest.IsolatedAsyncioTestCase):
    async def test_manual_force_during_running_summary_runs_full_read_afterwards(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("fixture", "pw")
            shop = store.list()[0]
            item = Session(shop, store)
            item.state = "logged_in"
            item.window = AsyncMock()
            service = BusinessService(store, lambda _: item)
            options = promotions.options_for()
            key = service.key("ad_reports", options, shop)
            entered, release = asyncio.Event(), asyncio.Event()
            modes = []
            service.demands.replace(shop, [("ad_reports", options, "首页核心数据")])

            async def reader(session, kind, scope, *args):
                summary = summary_publication.only(kind, scope)
                modes.append(summary)
                if summary:
                    entered.set()
                    await release.wait()
                    return {"rows": [], "metrics": {"total_spend": 8}, "summary_complete": True, "details_on_demand": True, "complete": False}
                return {"rows": [{"id": "manual-detail"}], "metrics": {"total_spend": 8}, "complete": True}

            try:
                with patch("backend.incremental_collection.read", side_effect=reader):
                    service.enqueue(shop, item, "ad_reports", options, refresh=True, foreground=False)
                    await asyncio.wait_for(entered.wait(), 5)
                    service.enqueue(shop, item, "ad_reports", options, force=True)
                    self.assertTrue(service.progress[sid, key]["repeat_force"])
                    release.set()
                    for _ in range(3):
                        if service.jobs:
                            await asyncio.gather(*list(service.jobs.values()))
                    self.assertFalse(service.jobs)
                self.assertEqual(modes, [True, False])
                self.assertEqual(store.read_cache(sid, key)["payload"]["rows"][0]["id"], "manual-detail")
            finally:
                release.set()
                await service.close()

    async def test_scheduled_summary_finishes_preserves_detail_checkpoint_and_does_not_requeue(self):
        for has_full in (False, True):
            with self.subTest(has_full=has_full), tempfile.TemporaryDirectory() as folder:
                store = Store(folder)
                sid = store.add("fixture", "pw")
                shop = store.list()[0]
                item = Session(shop, store)
                item.state = "logged_in"
                item.window = AsyncMock()
                service = BusinessService(store, lambda _: item)
                options = promotions.options_for()
                key = service.key("ad_reports", options, shop)
                before = None
                if has_full:
                    store.write_cache(sid, key, {"rows": [{"id": "old"}], "metrics": {"total_spend": 1}, "complete": True})
                    before = store.read_cache(sid, key)
                collection.register(store, sid, key, "ad_reports", options)
                checkpoint = Checkpoint(store, sid, key, {})
                checkpoint.data["proof"] = "keep original detail checkpoint"
                checkpoint.save()
                old_checkpoint = copy.deepcopy(checkpoint.data)

                async def reader(session, kind, scope, *args):
                    self.assertTrue(summary_publication.only(kind, scope))
                    return {"rows": [], "metrics": {"total_spend": 8, "ad_spend": 7}, "summary_complete": True, "details_on_demand": True, "complete": False}

                try:
                    with patch("backend.incremental_collection.read", side_effect=reader):
                        service.enqueue(shop, item, "ad_reports", options, refresh=True, foreground=False)
                        self.assertTrue(service.jobs)
                        await asyncio.gather(*list(service.jobs.values()))
                    state = collection.state(store, sid, key)
                    self.assertGreater(state["last_success"], time.time() - 30, state)
                    self.assertEqual(state["failures"], 0)
                    self.assertFalse(state["continuation"])
                    self.assertEqual(store.read_cache(sid, key), before)
                    self.assertEqual(Checkpoint(store, sid, key, {}).data["proof"], old_checkpoint["proof"])
                    source = service.data("ad_reports", options)["sources"][0]
                    self.assertTrue(source["details_on_demand"])
                    self.assertEqual(source["details_complete"], has_full)
                    self.assertEqual(source["details_updated_at"], before["updated"] if before else None)
                    module = next(m for m in home_progress.read(service)["rows"][0]["modules"] if m["kind"] == "ad_reports")
                    self.assertTrue(module["details_on_demand"])
                    self.assertTrue(module["today_updated"])
                    self.assertFalse(module["overdue"])
                    self.assertFalse(module["error"])
                    clocks, _ = scheduling_states(store, [(sid, key)])
                    self.assertIn((sid, key), clocks)
                    service.enqueue(shop, item, "ad_reports", options, refresh=True, foreground=False)
                    self.assertFalse(service.jobs, "fresh summary must wait for its existing frequency")
                finally:
                    await service.close()

    async def test_explicit_full_read_and_interrupted_full_retry_still_read_details(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("fixture", "pw")
            shop = store.list()[0]
            item = Session(shop, store)
            item.state = "logged_in"
            item.window = AsyncMock()
            service = BusinessService(store, lambda _: item)
            options = promotions.options_for()
            key = service.key("ad_reports", options, shop)
            collection.register(store, sid, key, "ad_reports", options)
            prior = Checkpoint(store, sid, key, {})
            prior.data["force_full"] = False
            prior.data["proof"] = "automatic detail checkpoint"
            prior.save()

            async def reader(session, kind, scope, *args):
                self.assertFalse(summary_publication.only(kind, scope))
                self.assertTrue(summary_publication.manual_details_pending(store, sid, key))
                return {"rows": [{"id": "new-detail"}], "metrics": {"total_spend": 9}, "complete": True}

            try:
                # Upgrading an old automatic continuation to a manual detail
                # request must survive a failure before the first page arrives.
                with patch("backend.incremental_collection.read", AsyncMock(side_effect=ValueError("detail read failed"))):
                    service.enqueue(shop, item, "ad_reports", options, force=True)
                    await asyncio.gather(*list(service.jobs.values()))
                self.assertTrue(summary_publication.manual_details_pending(store, sid, key))
                self.assertEqual(Checkpoint(store, sid, key, {}).data["proof"], "automatic detail checkpoint")
                with store.write_connection() as db:
                    db.execute("UPDATE collection_tasks SET retry_at=0 WHERE shop_id=? AND cache_key=?", (sid, key))
                with patch("backend.incremental_collection.read", side_effect=reader):
                    service.enqueue(shop, item, "ad_reports", options, refresh=True, foreground=False)
                    await asyncio.gather(*list(service.jobs.values()))
                self.assertEqual(store.read_cache(sid, key)["payload"]["rows"][0]["id"], "new-detail")
                self.assertFalse(summary_publication.manual_details_pending(store, sid, key))
                cp = Checkpoint(store, sid, key, {})
                cp.data["force_full"] = True
                cp.save()
                with patch("backend.incremental_collection.read", side_effect=reader):
                    service.enqueue(shop, item, "ad_reports", options, refresh=True, foreground=False)
                    await asyncio.gather(*list(service.jobs.values()))
                self.assertFalse(summary_publication.manual_details_pending(store, sid, key))
            finally:
                await service.close()
