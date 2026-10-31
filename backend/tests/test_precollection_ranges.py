import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend import collection, incremental_collection as inc, order_plan, task_lifecycle
from backend.business import BusinessService
from backend.store import Store


class RangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_orders_keep_old_rows_and_refresh_transitions(self):
        now = datetime.now(inc.BEIJING).replace(hour=18, minute=0, second=0, microsecond=0)
        scope = order_plan.day_scope(now.date().isoformat(), "recent")
        stamp = int(now.timestamp())
        text = lambda hour: now.replace(hour=hour).strftime("%Y-%m-%d %H:%M:%S")
        previous = {
            "rows": [
                {"id": "closed", "status": "交易成功", "created_at": text(1)},
                {"id": "open", "status": "已成团，待发货", "created_at": text(2)},
            ],
            "complete": True,
            "_sync": {"full_at": stamp, "snapshot_started_at": stamp},
        }
        calls = []

        async def reader(options):
            calls.append(options)
            if options.get("status"):
                rows = []
            elif options["start"] == stamp - inc.OVERLAP:
                rows = [{"id": "new", "status": "待付款", "created_at": text(18)}]
            else:
                rows = [{"id": "open", "status": "交易成功", "created_at": text(2)}]
            return {"rows": rows, "complete": True, "source_loaded": len(rows)}

        result = await inc.merge_delta("orders", scope, previous, inc.since(scope, previous), reader)
        self.assertEqual({r["id"] for r in result["rows"]}, {"closed", "open", "new"})
        self.assertEqual(next(r["status"] for r in result["rows"] if r["id"] == "open"), "交易成功")
        self.assertEqual(calls[0]["start"], stamp - 3600)
        self.assertEqual([c["status"] for c in calls[1:4]], list(order_plan.OPEN))

    async def test_partial_or_old_daily_snapshot_never_shrinks(self):
        scope = order_plan.day_scope(datetime.now(inc.BEIJING).date().isoformat(), "recent")
        session = SimpleNamespace(shop={"platform": "pdd"})
        reader = AsyncMock(return_value={"rows": [], "complete": True})
        for complete, full_at in ((False, time.time()), (True, 1)):
            previous = {
                "rows": [],
                "complete": complete,
                "_sync": {"full_at": full_at, "snapshot_started_at": time.time()},
            }
            with patch.object(inc, "list_delta", new=AsyncMock()) as delta:
                await inc.read(session, "orders", scope, {"payload": previous}, collection.DEFAULTS, False, reader)
                delta.assert_not_awaited()
        self.assertEqual(reader.await_count, 2)

    def test_distant_transitions_do_not_scan_intervening_months(self):
        self.assertEqual(
            inc.recheck_ranges([100, 200, 80 * 86400], {"start": 0, "end": 90 * 86400}),
            [(100, 200), (80 * 86400, 80 * 86400)],
        )
        self.assertEqual(len(inc.recheck_ranges([i * 86400 for i in range(10)], {"start": 0, "end": 20 * 86400})), 1)

    def test_yesterday_watermark_cannot_start_after_scope_end(self):
        self.assertEqual(inc.since({"start": 0, "end": 86400}, {"_sync": {"snapshot_started_at": 180000}}), 82800)

    def test_historical_day_rechecked_before_browser_request(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("fixture", "pw")
            shop = store.list()[0]
            service = BusinessService(store, lambda _: None)
            day = (datetime.now(inc.BEIJING).date() - timedelta(days=10)).isoformat()
            scope = order_plan.day_scope(day, "history")
            key = service.key("orders", scope, shop)
            collection.register(store, sid, key, "orders", scope)
            self.assertFalse(order_plan.covered(store, shop, scope))
            order_plan.publish(store, shop, scope, {"rows": [], "complete": False})
            self.assertFalse(order_plan.covered(store, shop, scope))
            order_plan.publish(store, shop, scope, {"rows": [], "complete": True})
            self.assertTrue(order_plan.covered(store, shop, scope))
            self.assertFalse(task_lifecycle.before_run(store, shop, "orders", scope, key, service.key, subscribed=True))
            # The all-orders/history task was retired; even force must not
            # resurrect it. Coverage remains readable for retained records.
            self.assertFalse(
                task_lifecycle.before_run(store, shop, "orders", scope, key, service.key, force=True, subscribed=True)
            )
            collection.register(store, sid, key, "orders", scope)
            with store.connect() as db:
                db.execute("UPDATE collection_tasks SET last_success=0 WHERE cache_key=?", (key,))
            self.assertFalse(task_lifecycle.before_run(store, shop, "orders", scope, key, service.key, subscribed=True))
            self.assertFalse(task_lifecycle.reconcile(store, shop, set(), service.key, set()))

    async def test_shared_order_page_consumes_initial_response_only_once(self):
        from backend.orders import OrdersPage, collect

        page = object.__new__(OrdersPage)
        page.order_responses = asyncio.Queue()
        page.order_responses.put_nowait(({}, {"success": True, "result": {}}))
        scope = order_plan.day_scope(datetime.now(inc.BEIJING).date().isoformat(), "recent")
        with (
            patch("backend.orders.checked_result"),
            patch("backend.orders.collect_single", new=AsyncMock(return_value={"rows": [], "complete": True})),
        ):
            await collect(page, scope)
            await asyncio.wait_for(collect(page, {**scope, "status": "待发货"}), 1)
        self.assertTrue(page.initial_order_checked)
