import asyncio, copy, json, tempfile, time, unittest
from types import SimpleNamespace
from backend import collection, task_lifecycle
from backend.store import Store
from backend.frequency_policy import catalog, group
from backend.home_only_pause import apply, held, MARKER, REVISION, BACKUP
from backend.task_catalog import HOME, grouped_id, assign
from backend.business import BusinessService
from backend.collection_checkpoint import Checkpoint


class HomeOnlyPauseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.config = collection.settings(self.store)

    def test_existing_policy_migrates_once_with_backup_and_preserved_home_limits(self):
        old = copy.deepcopy(self.config)
        old.pop(MARKER, None)
        old.pop("task_catalog", None)
        old.update(max_concurrent=17, shop_concurrent=4, shop_limits={"1": 2})
        assign(old, "pdd", "overview@home", "realtime")
        assign(old, "pdd", "products@base", "daily")
        assign(old, "pdd", "orders@recent", "custom2")
        with self.store.connect() as db:
            db.execute("DELETE FROM settings WHERE key=?", (BACKUP,))
            db.execute("UPDATE settings SET value=? WHERE key='collection_policy_v3'", (json.dumps(old),))
        result = collection.settings(self.store)
        self.assertEqual(result[MARKER], REVISION)
        for field in (
            "frequency_groups",
            "max_concurrent",
            "shop_concurrent",
            "shop_limits",
            "platform_modes",
            "enabled",
        ):
            self.assertEqual(result[field], old[field])
        for key in HOME | {grouped_id(k) for k in HOME}:
            self.assertEqual(result["task_modes"]["pdd"].get(key), old["task_modes"]["pdd"].get(key))
        for p in result["task_modes"]:
            for task in catalog():
                if task["id"] not in {grouped_id(k) for k in HOME}:
                    self.assertEqual(result["task_modes"][p][task["id"]], "off")
        with self.store.connect() as db:
            saved = json.loads(db.execute("SELECT value FROM settings WHERE key=?", (BACKUP,)).fetchone()[0])
        self.assertEqual(saved, old)

    def test_manual_reenable_survives_settings_reads_and_restart(self):
        c = collection.save_settings(
            self.store,
            {"module_assignments": [{"platform": "pdd", "kind": "orders@recent", "tab": "task", "target": "custom2"}]},
        )
        for _ in range(2):
            self.assertEqual(collection.settings(self.store), c)
        self.assertEqual(collection.settings(Store(self.tmp.name)), c)
        self.assertIsNotNone(group("orders", {"_order_lane": "recent", "date_from": "2026-01-01"}, c, "pdd"))
        self.assertIsNone(group("packages", {}, c, "pdd"))

    def test_home_scopes_continue_non_home_failures_and_force_cannot_admit(self):
        for kind, scope in [
            ("overview", {}),
            ("packages", {"_attention": True}),
            ("aftersales", {"status": "待处理即将逾期"}),
            ("products", {}),
        ]:
            self.assertFalse(held(kind, scope, self.config, "pdd"))
        for kind, scope in [("packages", {}), ("orders", {"status": "待发货"}), ("goodsdata", {})]:
            self.assertTrue(held(kind, scope, self.config, "pdd"))
            cached = {"payload": {"rows": [1]}}
            self.assertIs(
                BusinessService.admit(
                    None,
                    {"platform": "pdd"},
                    None,
                    kind,
                    (scope, "key", cached, self.config, {"failures": 4, "continuation": True}),
                    True,
                    True,
                    True,
                ),
                cached,
            )
        self.assertFalse(
            task_lifecycle.before_run(
                self.store, {"platform": "pdd"}, "packages", {}, "key", None, force=True, config=self.config
            )
        )

    def test_pause_preserves_business_rows_and_old_checkpoint(self):
        sid = self.store.add("fixture", "pw")
        shop = self.store.list()[0]
        service = BusinessService(self.store, lambda _: None)
        scope = {}
        key = service.key("packages", scope, shop)
        collection.register(self.store, sid, key, "packages", scope)
        self.store.write_cache(sid, key, {"rows": [{"id": "kept"}]})
        checkpoint = Checkpoint(self.store, sid, key, {})
        checkpoint.save()
        with self.store.connect() as db:
            db.execute("UPDATE collection_checkpoints SET updated=1 WHERE shop_id=?", (sid,))
        task_lifecycle.reconcile(self.store, shop, set(), service.key, set(), time.time())
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM collection_checkpoints WHERE shop_id=?", (sid,)).fetchone()[0], 1
            )
        self.assertEqual(self.store.read_cache(sid, key)["payload"]["rows"][0]["id"], "kept")


class QueuePauseTests(unittest.IsolatedAsyncioTestCase):
    async def test_forced_queued_read_cancelled_active_read_kept(self):
        service = BusinessService(None, lambda _: None)
        from backend.frequency_policy import migrate

        config = apply(migrate(copy.deepcopy(collection.DEFAULTS)), catalog())
        queued = asyncio.create_task(asyncio.sleep(20))
        active = asyncio.create_task(asyncio.sleep(20))
        service.progress = {
            (1, "queued"): {"kind": "packages", "scope": {}, "phase": "queued", "force": True},
            (1, "active"): {"kind": "packages", "scope": {}, "phase": "running"},
        }
        service.jobs = {(1, "queued"): queued, (1, "active"): active}
        service.configure_queue(config)
        await asyncio.gather(queued, return_exceptions=True)
        self.assertTrue(queued.cancelled())
        self.assertFalse(active.done())
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
