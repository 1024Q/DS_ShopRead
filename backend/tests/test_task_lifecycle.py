import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend import collection, order_plan, task_lifecycle as lifecycle
from backend.business import BusinessService, options_for
from backend.store import Store
from backend.collection_checkpoint import Checkpoint


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("fixture", "pw")
        self.shop = self.store.list()[0]
        self.service = BusinessService(self.store, lambda _: None)
        from backend.tests.collection_fixture import assign

        assign(self.store, {"reviews@base": "custom"})

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, kind, scope):
        key = self.service.key(kind, scope, self.shop)
        collection.register(self.store, self.sid, key, kind, scope)
        return key

    def test_expired_failed_query_archived_without_deleting_saved_data(self):
        scope = options_for("reviews", date_from="2026-01-01", date_to="2026-01-02")
        key = self.register("reviews", scope)
        self.store.write_cache(self.sid, key, {"rows": [], "complete": True})
        checkpoint = Checkpoint(self.store, self.sid, key, {})
        checkpoint.save()
        with self.store.connect() as db:
            db.execute("UPDATE collection_tasks SET demand_until=1,failures=4 WHERE cache_key=?", (key,))
        lifecycle.reconcile(self.store, self.shop, set(), self.service.key, set())
        state = collection.state(self.store, self.sid, key)
        self.assertEqual(state["schedule_state"], "archived")
        self.assertEqual(state["continuation"], 0)
        self.assertIsNotNone(self.store.read_cache(self.sid, key))
        self.assertFalse(lifecycle.before_run(self.store, self.shop, "reviews", scope, key, self.service.key))

    def test_focus_never_expires_while_subscribed(self):
        scope = {**options_for("evidence"), "_attention": True}
        key = self.register("evidence", scope)
        with self.store.connect() as db:
            db.execute("UPDATE collection_tasks SET demand_until=1,failures=30 WHERE cache_key=?", (key,))
        kept = lifecycle.reconcile(self.store, self.shop, {key}, self.service.key, set())
        self.assertEqual([r["cache_key"] for r in kept], [key])
        self.assertTrue(
            lifecycle.before_run(self.store, self.shop, "evidence", scope, key, self.service.key, subscribed=True)
        )

    def test_disabled_order_history_is_not_restored_by_continuation(self):
        day = (order_plan.datetime.now(order_plan.BEIJING).date() - timedelta(days=20)).isoformat()
        scope = order_plan.day_scope(day, "history")
        record = {"kind": "orders", "last_success": 0, "demand_until": 1}
        self.assertFalse(lifecycle.retained(record, scope))
        self.assertFalse(lifecycle.retained({**record, "continuation": True}, scope, fixed=True))
        self.assertFalse(lifecycle.retained({**record, "last_success": time.time()}, scope))
        old = {**scope, "date_from": "2020-01-01", "date_to": "2020-01-01"}
        self.assertFalse(lifecycle.retained(record, old))

    def test_stale_checkpoint_resets_but_active_work_is_not_touched(self):
        scope = options_for("tickets")
        key = self.register("tickets", scope)
        Checkpoint(self.store, self.sid, key, {}).save()
        with self.store.connect() as db:
            db.execute("UPDATE collection_checkpoints SET updated=1")
        lifecycle.reconcile(self.store, self.shop, {key}, self.service.key, {key})
        self.assertEqual(collection.state(self.store, self.sid, key)["continuation"], 1)
        lifecycle.reconcile(self.store, self.shop, {key}, self.service.key, set())
        self.assertEqual(collection.state(self.store, self.sid, key)["continuation"], 0)

    def test_account_change_prevents_old_queued_work(self):
        scope = options_for("tickets")
        key = self.register("tickets", scope)
        with self.store.connect() as db:
            db.execute("UPDATE shops SET profile_version=profile_version+1 WHERE id=?", (self.sid,))
        self.assertFalse(
            lifecycle.before_run(
                self.store, self.shop, "tickets", scope, key, self.service.key, force=True, subscribed=True
            )
        )

    def test_atomic_cache_retry_reuses_payload_and_rolls_back_partial_changes(self):
        scope = options_for("reviews")
        key = self.register("reviews", scope)
        from backend import table_index

        original = table_index.write
        calls = []

        def write(*args, **kwargs):
            original(*args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")

        payload = {"rows": [{"id": "one", "name": "test"}], "complete": True}
        with patch.object(table_index, "write", side_effect=write):
            self.store.write_cache(self.sid, key, payload)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.read_cache(self.sid, key)["payload"], payload)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM table_rows WHERE cache_key=?", (key,)).fetchone()[0], 1)

    def test_deferred_details_explain_waits_and_cooldown(self):
        from backend.collector_task_list import listing

        self.service.deferred = {
            (self.sid, "test"): {
                "kind": "tickets",
                "scope": {},
                "stage": "重试冷却",
                "waiting_since": time.time() - 120,
                "retry_at": time.time() + 30,
                "reason": "网络暂不可用",
            }
        }
        result = listing(self.service, "deferred", 1, {}, {}, [])
        self.assertEqual(result["categories"], {"重试冷却": 1})
        self.assertIn("后重试", result["rows"][0]["detail"])


class ReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_subscription_passes_execution_gate_and_expired_query_does_not(self):
        from backend.scheduler import Scheduler

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("fixture", "pw")
            shop = store.list()[0]
            item = SimpleNamespace(retired=False, state="logged_in")
            service = BusinessService(store, lambda _: item)
            scheduler = Scheduler(service)
            scheduler.waiting = {}
            config = {**collection.settings(store), "keepalive_enabled": False, "stagger_seconds": 0}
            scope = options_for("reviews", date_from="2026-01-01", date_to="2026-01-02")
            key = service.key("reviews", scope, shop)
            collection.register(store, sid, key, "reviews", scope)
            with store.connect() as db:
                db.execute("UPDATE collection_tasks SET demand_until=1,failures=3 WHERE cache_key=?", (key,))
            await scheduler.tick_shop(shop, config)
            self.assertEqual(collection.state(store, sid, key)["schedule_state"], "archived")
            candidate = next(c for c in scheduler.candidates if c[2] == "overview")
            _, _, kind, options, _ = candidate
            _, fresh_key, _, _, _ = service.prepare_enqueue(shop, kind, options, False)
            self.assertTrue(lifecycle.before_run(store, shop, kind, options, fresh_key, service.key, subscribed=True))

    async def test_concurrent_identical_complete_query_fetches_once(self):
        from backend.request_reuse import complete

        session = SimpleNamespace(shop={"platform": "pdd"}, state="logged_in")

        async def fetch():
            await asyncio.sleep(0.02)
            return {"rows": [], "complete": True}

        reader = AsyncMock(side_effect=fetch)
        await asyncio.gather(*(complete(session, "tickets", options_for("tickets"), reader) for _ in range(3)))
        self.assertEqual(reader.await_count, 1)
