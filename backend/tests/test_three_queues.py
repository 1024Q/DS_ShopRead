"""Frequency groups share node capacity; they do not reserve independent slots."""

import json, tempfile, unittest
from backend import collection
from backend.store import Store
from backend.collection_queue import select_jobs, reservations, queue_for
from backend.tests.collection_fixture import assign, policy


class FrequencyQueueTests(unittest.TestCase):
    def test_each_group_can_use_available_node_capacity(self):
        c = policy(max_concurrent=8, shop_concurrent=3, max_active_shops=8)
        for group in ("realtime", "custom", "daily", "custom2"):
            c["task_modes"] = {"pdd": {"products@base": group}}
            tasks = {
                (sid, "products"): {"kind": "products", "phase": "queued", "queued_at": 1, "scope": {}}
                for sid in range(1, 11)
            }
            self.assertEqual(len(select_jobs(tasks, c, now=1000)), 8)
            self.assertEqual(set(queue_for(m, c) for m in tasks.values()), {group})
            self.assertTrue(all(v == 0 for v in reservations(c).values()))

    def test_configuration_patch_does_not_change_cadence_or_sleep(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            before = assign(store, {"orders@unpaid": "custom2", "products@base": "daily"})
            after = collection.save_settings(store, {"shop_concurrent": 4, "max_concurrent": 25})
            for key in ("task_modes", "frequency_groups", "browser_idle_seconds", "max_active_shops"):
                self.assertEqual(after[key], before[key])
            self.assertEqual(collection.settings(Store(folder)), after)
            with self.assertRaises(ValueError):
                collection.save_settings(store, {"scheduled_concurrent": 3})
            self.assertEqual(collection.settings(store), after)

    def test_existing_policy_record_is_preserved_during_data_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            original = {**collection.DEFAULTS, "max_concurrent": 16, "daily_time": "05:00"}
            with store.connect() as db:
                db.execute("DELETE FROM settings WHERE key='collection_policy_v3'")
                db.execute(
                    "INSERT OR REPLACE INTO settings(key,value) VALUES('collection_policy',?)", (json.dumps(original),)
                )
            current = collection.settings(store)
            self.assertEqual(current["frequency_version"], 1)
            with store.connect() as db:
                self.assertEqual(
                    json.loads(db.execute("SELECT value FROM settings WHERE key='collection_policy'").fetchone()[0]),
                    original,
                )
