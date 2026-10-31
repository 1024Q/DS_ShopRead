import asyncio, copy, tempfile, time, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from backend import collection
from backend.frequency_policy import migrate
from backend.master_tasks import representatives, project, counts, remember, listing
from backend.collection_queue import select_jobs, queue_status, waiting_reason
from backend.business import BusinessService, options_for
from backend.shop_lock import ShopLock


class MasterSelectionTests(unittest.TestCase):
    def setUp(self):
        self.config = migrate(copy.deepcopy(collection.DEFAULTS))

    def candidate(self, kind, scope=None, attempt=0, sid=1):
        return ({"id": sid, "platform": "pdd"}, None, kind, scope or {}, {"last_attempt": attempt, "waiting_since": 1})

    def test_independent_businesses_admitted_together_and_same_business_rotates(self):
        candidates = [
            self.candidate("ad_products", attempt=100),
            self.candidate("goodsdata", {"trade_range": "realtime"}, attempt=20),
            self.candidate("goodsdata", {"trade_range": "yesterday"}, attempt=10),
            self.candidate("products"),
        ]
        chosen, deferred = representatives(candidates, {}, self.config, 200)
        self.assertEqual({c[2] for c in chosen}, {"ad_products", "goodsdata", "products"})
        self.assertEqual([c[3] for c in chosen if c[2] == "goodsdata"], [{"trade_range": "yesterday"}])
        self.assertEqual([c[3] for c in deferred], [{"trade_range": "realtime"}])
        progress = {(1, "goodsdata:a"): {"kind": "goodsdata", "phase": "running", "scope": {}}}
        chosen, deferred = representatives(candidates, progress, self.config, 200)
        self.assertEqual([c[2] for c in chosen], ["ad_products", "products"])
        self.assertEqual(len(deferred), 2)

    def test_same_master_allows_independent_businesses_but_not_same_kind(self):
        meta = lambda k, s={}: dict(kind=k, scope=s, phase="queued", queued_at=1)
        progress = {
            (1, "ad_products:a"): meta("ad_products"),
            (1, "goodsdata:a"): meta("goodsdata"),
            (1, "aftersales:a"): meta("aftersales"),
            (1, "aftersales:b"): meta("aftersales", {"_attention": True}),
            (2, "goodsdata:a"): meta("goodsdata"),
        }
        chosen = select_jobs(progress, {**self.config, "max_concurrent": 10, "shop_concurrent": 10}, now=100)
        self.assertEqual(len(chosen), 4)
        self.assertEqual(sum(k[1].split(":")[0] in ("ad_products", "goodsdata") and k[0] == 1 for k in chosen), 2)
        self.assertEqual(sum(k[1].startswith("aftersales") for k in chosen), 1)

    def test_parallel_steps_respect_node_shop_override_and_shop_rotation_limits(self):
        progress = {
            (sid, kind): dict(kind=kind, scope={}, phase="queued", queued_at=1)
            for sid in range(1, 4)
            for kind in ("overview", "transactions", "ad_reports", "orders")
        }
        config = {
            **self.config,
            "max_concurrent": 20,
            "shop_concurrent": 4,
            "max_active_shops": 2,
            "shop_limits": {"1": 2},
        }
        original = copy.deepcopy(config)
        selected = select_jobs(progress, config, now=100)
        self.assertEqual(len(selected), 6)
        self.assertEqual({sid for sid, _ in selected}, {1, 2})
        self.assertEqual(sum(sid == 1 for sid, _ in selected), 2)
        self.assertEqual(config, original)
        self.assertEqual(len(select_jobs(progress, {**config, "max_concurrent": 3}, now=100)), 3)

    def test_parent_projection_does_not_double_count_internal_waiters(self):
        service = SimpleNamespace(
            queue_config=self.config,
            progress={
                (1, "ad_products:a"): {
                    "kind": "ad_products",
                    "scope": {},
                    "phase": "running",
                    "started_at": time.time(),
                }
            },
            deferred={(1, "goodsdata:a"): {"kind": "goodsdata", "scope": {}, "stage": "主任务内等待子步骤轮转"}},
        )
        rows = project(service)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["step_count"], 2)
        self.assertEqual(counts(rows), dict(running=1, queued=0, deferred=0, waiting=0))
        service.progress[1, "goodsdata:a"] = {
            "kind": "goodsdata",
            "scope": {},
            "phase": "queued",
            "queued_at": time.time(),
        }
        queues = queue_status(service.progress, self.config)
        self.assertEqual(sum(v["queued"] for v in queues.values() if isinstance(v, dict)), 1)

    def test_task_list_shows_all_parallel_steps_and_distinct_deadlines(self):
        now = time.time()
        service = SimpleNamespace(
            queue_config=self.config,
            store=SimpleNamespace(list=lambda: [{"id": 1, "name": "测试店铺"}]),
            deferred={},
            progress={
                (1, "ad_products:a"): {
                    "kind": "ad_products",
                    "scope": {},
                    "phase": "running",
                    "started_at": now - 5,
                    "read_deadline": now + 30,
                    "stage": "读取推广",
                },
                (1, "goodsdata:a"): {
                    "kind": "goodsdata",
                    "scope": {},
                    "phase": "running",
                    "started_at": now - 2,
                    "read_deadline": now + 10,
                    "stage": "读取商品",
                },
            },
        )
        rows = project(service)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["running_steps"], 2)
        self.assertIn("商品推广", rows[0]["current_step"])
        self.assertIn("商品数据", rows[0]["current_step"])
        self.assertEqual(counts(rows)["running"], 1)
        self.assertEqual(
            sum(v["running"] for v in queue_status(service.progress, self.config).values() if isinstance(v, dict)), 2
        )
        row = listing(service, "running", 1)["rows"][0]
        self.assertIn("同时执行 2 个子步骤", row["stage"])
        self.assertIn("读取推广", row["detail"])
        self.assertIn("读取商品", row["detail"])
        self.assertEqual(len(row["steps"]), 2)
        self.assertIn("商品推广", row["remaining"])
        self.assertIn("商品数据", row["remaining"])

    def test_wait_reason_only_blocks_same_business_or_actual_capacity(self):
        progress = {
            (1, "overview"): {"kind": "overview", "scope": {}, "phase": "running", "started_at": time.time() - 1},
            (1, "transactions"): {"kind": "transactions", "scope": {}, "phase": "queued", "queued_at": 1},
        }
        key = (1, "transactions")
        meta = progress[key]
        self.assertEqual(waiting_reason(key, meta, progress, self.config), "等待调度补位")
        self.assertEqual(waiting_reason(key, meta, progress, {**self.config, "max_concurrent": 1}), "采集并发已满")
        meta["kind"] = "overview"
        self.assertIn("等待同店同模块", waiting_reason(key, meta, progress, self.config))

    def test_restart_uses_independent_durable_success_failure_and_checkpoint(self):
        from backend.store import Store
        from backend.collection_checkpoint import Checkpoint

        with tempfile.TemporaryDirectory() as path:
            store = Store(path)
            sid = store.add("test", "pw")
            for kind in ("ad_products", "goodsdata"):
                collection.register(store, sid, kind + ":test", kind, {})
            collection.finish(store, sid, "ad_products:test")
            checkpoint = Checkpoint(store, sid, "goodsdata:test", {})
            checkpoint.data["parts"]["saved"] = {
                "ids": ["a"],
                "rows": {"a": {"id": "a"}},
                "loaded": 1,
                "total": 2,
                "next": 2,
                "done": False,
            }
            checkpoint.save()
            collection.finish(store, sid, "goodsdata:test", error="fixture", error_kind="network")
            service = SimpleNamespace(queue_config=self.config, progress={}, deferred={})
            for kind in ("ad_products", "goodsdata"):
                remember(service, sid, kind + ":test", kind, {}, collection.state(store, sid, kind + ":test"))
            rows = project(service)
            self.assertEqual(len(rows), 1)
            steps = {s["kind"]: s for s in rows[0]["steps"]}
            self.assertGreater(steps["ad_products"]["last_success"], 0)
            self.assertEqual(steps["ad_products"]["failures"], 0)
            self.assertEqual(steps["goodsdata"]["failures"], 1)
            self.assertTrue(steps["goodsdata"]["continuation"])
            self.assertEqual(Checkpoint(store, sid, "goodsdata:test", {}).data["parts"]["saved"]["next"], 2)


class MasterExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_admits_independent_steps_and_refills_same_kind_without_scan(self):
        from backend.scheduler import Scheduler

        service = BusinessService(None, None)
        service.queue_config = migrate(copy.deepcopy(collection.DEFAULTS))
        scheduler = Scheduler(service)
        shop = {"id": 1, "platform": "pdd", "profile_version": 0}
        item = SimpleNamespace(retired=False, state="logged_in")
        scheduler.pending = [
            (shop, item, k, o, dict(waiting_since=1))
            for k, o in [
                ("ad_products", {}),
                ("goodsdata", {}),
                ("products", {}),
                ("goodsdata", {"trade_range": "yesterday"}),
            ]
        ]

        def prepare(candidates, config):
            return [(s, i, k, (o, service.key(k, o, s), None, config, {}), t) for s, i, k, o, t in candidates]

        def admit(s, i, k, plan, *args):
            identity = (s["id"], plan[1])
            service.jobs[identity] = object()
            service.progress[identity] = {"kind": k, "scope": plan[0], "phase": "queued", "queued_at": 1}

        service.prepare_scheduled = prepare
        service.admit = admit
        await scheduler.refill()
        self.assertEqual(len(service.progress), 4)
        self.assertEqual(len(scheduler.pending), 0)
        selected = next(k for k, m in service.progress.items() if m["kind"] == "goodsdata")
        service.progress.pop(selected)
        service.jobs.pop(selected)
        await scheduler.refill()
        self.assertEqual(len(service.progress), 3)
        self.assertEqual(scheduler.pending, [])

    async def test_same_master_parallel_same_business_waits_and_immediately_refills(self):
        service = BusinessService(None, None)
        service.queue_config = migrate(copy.deepcopy(collection.DEFAULTS))
        service.queue_config.update(max_concurrent=3, shop_concurrent=3)
        item = SimpleNamespace(lock=ShopLock(), shop={"id": 1})
        entered = [asyncio.Event() for _ in range(4)]
        release = [asyncio.Event() for _ in range(4)]

        async def run(i, kind):
            async with service.slot((1, kind + ":" + str(i)), item, kind):
                entered[i].set()
                await release[i].wait()

        jobs = [
            asyncio.create_task(run(i, k)) for i, k in enumerate(("ad_products", "goodsdata", "products", "goodsdata"))
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*(entered[i].wait() for i in (0, 1, 2))), 1)
            self.assertFalse(entered[3].is_set())
            release[1].set()
            await asyncio.wait_for(entered[3].wait(), 0.5)
            self.assertFalse(release[0].is_set())
            self.assertFalse(release[2].is_set())
        finally:
            for event in release:
                event.set()
            await asyncio.gather(*jobs)
            await service.close()

    async def test_success_is_published_before_sibling_failure_without_replaying_success(self):
        from backend.store import Store
        from backend.browser import Session

        with tempfile.TemporaryDirectory() as path:
            store = Store(path)
            sid = store.add("test", "pw")
            shop = store.list()[0]
            item = Session(shop, store)
            item.state = "logged_in"
            item.window = AsyncMock()
            service = BusinessService(store, lambda _: item)
            calls = []
            published = asyncio.Event()
            loop = asyncio.get_running_loop()

            async def reader(session, kind, scope, *args):
                calls.append(kind)
                if kind == "transactions":
                    await asyncio.wait_for(published.wait(), 1)
                    self.assertIsNotNone(
                        store.read_cache(sid, service.key("overview", {"trade_range": "realtime"}, shop))
                    )
                    self.assertGreater(notify.call_count, 0)
                    raise ValueError("fixture failure")
                return {"rows": [{"id": "one"}], "complete": True}

            try:
                with (
                    patch("backend.incremental_collection.read", side_effect=reader),
                    patch(
                        "backend.center.delivery_notify.notify",
                        side_effect=lambda *_: loop.call_soon_threadsafe(published.set),
                    ) as notify,
                ):
                    for kind in ("overview", "transactions"):
                        scope = {"trade_range": "realtime"}
                        cache_key = service.key(kind, scope, shop)
                        collection.register(store, sid, cache_key, kind, scope)
                        service.enqueue(shop, item, kind, scope, force=True)
                    try:
                        await asyncio.wait_for(asyncio.gather(*list(service.jobs.values())), 5)
                    except TimeoutError:
                        self.fail("Parallel steps did not finish: " + repr(service.progress))
                    first = service.key("overview", {"trade_range": "realtime"}, shop)
                    second = service.key("transactions", {"trade_range": "realtime"}, shop)
                    self.assertIsNotNone(store.read_cache(sid, first), collection.state(store, sid, first))
                    self.assertGreater(collection.state(store, sid, first)["last_success"], 0)
                    self.assertEqual(collection.state(store, sid, second)["failures"], 1)
                    self.assertEqual(set(calls), {"overview", "transactions"})
                    self.assertEqual(len(calls), 2)
                    self.assertGreater(notify.call_count, 0)
            finally:
                await service.close()
