import asyncio, tempfile, time, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta
from backend.business import BusinessService, options_for
from backend import (
    collection,
    collection_checkpoint as cp,
    collection_plan,
    incremental_collection as inc,
    request_reuse,
)
from backend.store import Store
from backend.operations import normalize_operation


class EfficiencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("fixture", "pw")
        self.shop = self.store.list()[0]
        self.service = BusinessService(self.store, lambda _: None)

    def tearDown(self):
        self.temp.cleanup()

    def test_rolling_identity_and_explicit_history_are_separate(self):
        for kind in ("tickets", "evidence", "invoices", "packages", "reviews", "aftersales", "intercepts"):
            today = options_for(kind)
            tomorrow = {
                **today,
                "start": today["start"] + 86400,
                "end": today["end"] + 86400,
                "date_from": "2099-01-01",
                "date_to": "2099-01-02",
            }
            self.assertEqual(
                self.service.key(kind, today, self.shop), self.service.key(kind, tomorrow, self.shop), kind
            )
            self.assertNotEqual(
                self.service.key(kind, today, self.shop),
                self.service.key(kind, {**today, "_exact_dates": True}, self.shop),
                kind,
            )
        old = options_for("evidence")
        old["start"] -= 86400
        self.assertEqual(collection_plan.roll_window(old)["start"], options_for("evidence")["start"])

    async def test_shared_request_is_single_flight_and_isolated(self):
        root = SimpleNamespace(shop={"profile_version": 0}, state="logged_in", port=9000)

        async def remote():
            await asyncio.sleep(0.01)
            return {"rows": [1]}

        fetch = AsyncMock(side_effect=remote)
        a, b = await asyncio.gather(
            request_reuse.read(root, "/mercury/mms/afterSales/queryList", {"p": 1}, fetch),
            request_reuse.read(SimpleNamespace(session=root), "/mercury/mms/afterSales/queryList", {"p": 1}, fetch),
        )
        self.assertEqual(fetch.await_count, 1)
        a["rows"].append(2)
        self.assertEqual(b["rows"], [1])
        with request_reuse.fresh():
            await request_reuse.read(root, "/mercury/mms/afterSales/queryList", {"p": 1}, fetch)
        await request_reuse.read(root, "/mercury/mms/afterSales/queryList", {"p": 2}, fetch)
        await request_reuse.read(SimpleNamespace(shop={}, state="logged_in"), "/mercury/mms/afterSales/queryList", {"p": 1}, fetch)
        self.assertEqual(fetch.await_count, 4)
        bad = AsyncMock(side_effect=ValueError("failure"))
        for _ in range(2):
            with self.assertRaises(ValueError):
                await request_reuse.read(root, "/bad", {}, bad)
        self.assertEqual(bad.await_count, 2)
        for key, (stamp, value, size) in list(root._merchant_reads.items()):
            root._merchant_reads[key] = (stamp - 31, value, size)
        await request_reuse.read(root, "/mercury/mms/afterSales/queryList", {"p": 1}, fetch)
        self.assertEqual(fetch.await_count, 5)

    async def test_single_page_skips_verification_multi_page_bypasses_cache(self):
        async def run(total):
            calls = []

            async def fetch(n):
                calls.append((n, request_reuse._bypass.get()))
                return {"rows": [{"id": str(n)}], "ids": [str(n)], "total": total}

            with cp.context(self.store, self.sid, "fixture:" + str(total), {**collection.DEFAULTS, "batch_pages": 12}):
                result = await cp.paginate("part", fetch, lambda *a: None)
            return calls, result

        calls, result = await run(1)
        self.assertEqual(calls, [(1, False)])
        self.assertTrue(result["complete"])
        calls, _ = await run(2)
        self.assertEqual(calls, [(1, False), (2, False), (1, True)])

    def test_adaptive_resets_on_change_and_does_not_delay_active_focus(self):
        options = options_for("tickets")
        key = self.service.key("tickets", options, self.shop)
        collection.register(self.store, self.sid, key, "tickets", options)
        payload = {"complete": True, "rows": []}
        config = collection.DEFAULTS
        for _ in range(4):
            collection.record_change(self.store, self.sid, key, "tickets", options, payload, config)
        self.assertEqual(collection.state(self.store, self.sid, key)["adaptive_seconds"], 120)
        for _ in range(3):
            collection.record_change(self.store, self.sid, key, "tickets", options, payload, config)
        self.assertEqual(collection.state(self.store, self.sid, key)["adaptive_seconds"], 300)
        self.assertTrue(collection.due("tickets", options, 100, config, now=401, adaptive_seconds=300))
        self.assertFalse(collection.due("tickets", options, 100, config, now=200, adaptive_seconds=300))
        self.assertTrue(
            collection.due(
                "tickets", options, 100, {**config, "change_adaptive_enabled": False}, now=200, adaptive_seconds=300
            )
        )
        payload["rows"] = [{"id": "changed", "priority": 3}]
        for _ in range(8):
            collection.record_change(self.store, self.sid, key, "tickets", options, payload, config)
        self.assertEqual(collection.state(self.store, self.sid, key)["adaptive_seconds"], 0)

    def test_only_confirmed_aftersales_are_closed_and_order_time_is_preserved(self):
        self.assertFalse(inc.is_open("aftersales", {"status": "补寄， 确认完成补寄"}))
        self.assertTrue(inc.is_open("aftersales", {"status": "新状态"}))
        self.assertTrue(inc.is_open("aftersales", {"status": "退款，待商家处理"}))
        item = {
            "orderSn": "fixture",
            "orderTime": 1791849600000,
            "shipList": [{"shippingId": 1, "trackingNumber": "test", "interceptStatus": 2}],
        }
        self.assertTrue(normalize_operation("intercepts", item)[0]["created_at"])
        del item["orderTime"]
        self.assertEqual(normalize_operation("intercepts", item)[0]["created_at"], "")

    async def test_rollover_prunes_old_rows_but_keeps_all_age_invoices(self):
        options = options_for("evidence")
        now = datetime.fromtimestamp(options["start"], inc.BEIJING)
        previous = {
            "complete": True,
            "rows": [
                {"id": "old", "created_at": (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")},
                {"id": "keep", "created_at": now.strftime("%Y-%m-%d %H:%M:%S")},
            ],
            "_sync": {"full_at": time.time(), "snapshot_started_at": time.time() - 60},
        }
        with patch.object(inc, "list_delta", new=AsyncMock(return_value={})) as delta:
            await inc.read(
                SimpleNamespace(shop={"platform": "pdd"}),
                "evidence",
                options,
                {"payload": previous},
                collection.DEFAULTS,
                False,
                AsyncMock(),
            )
            self.assertEqual([r["id"] for r in delta.await_args.args[3]["rows"]], ["keep"])

    async def test_unknown_aftersale_is_rechecked_until_confirmed_closed(self):
        options = options_for("aftersales")
        stamp = datetime.fromtimestamp(options["start"] + 100, inc.BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        old = {
            "rows": [
                {"id": "closed", "status": "退款，退款成功", "created_at": stamp},
                {"id": "unknown", "status": "新状态", "created_at": stamp},
            ],
            "_sync": {"full_at": time.time()},
        }
        calls = []

        async def reader(value):
            calls.append(value)
            return {
                "complete": True,
                "rows": [{"id": "unknown", "status": "退款，退款成功", "created_at": stamp}] if len(calls) > 1 else [],
            }

        data = await inc.merge_delta("aftersales", options, old, options["start"] + 500, reader)
        self.assertEqual(len(calls), 2)
        self.assertFalse(inc.is_open("aftersales", next(r for r in data["rows"] if r["id"] == "unknown")))
        self.assertTrue(inc.supported(SimpleNamespace(shop={"platform": "pdd"}), "aftersales", options))
