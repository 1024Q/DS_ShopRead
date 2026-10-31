"""Removed priority settings must not revive a second scheduling authority."""

import tempfile, unittest
from backend import collection
from backend.store import Store
from backend.collection_queue import select_jobs
from backend.tests.collection_fixture import policy


class PriorityTests(unittest.TestCase):
    def test_obsolete_priority_writes_are_rejected_without_partial_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            before = collection.settings(store)
            for payload in (
                {"platform_priorities": {"pdd": {"orders": "high"}}},
                {
                    "module_assignments": [
                        {"platform": "pdd", "kind": "overview@home", "tab": "priority", "target": "high"}
                    ]
                },
            ):
                with self.assertRaises(ValueError):
                    collection.save_settings(store, payload)
                self.assertEqual(collection.settings(store), before)

    def test_stale_priority_metadata_cannot_overtake_an_older_due_task(self):
        config = policy(max_concurrent=1)
        tasks = {
            (1, "old"): {"kind": "products", "scope": {}, "phase": "queued", "queued_at": 1},
            (2, "new"): {
                "kind": "reviews",
                "scope": {},
                "phase": "queued",
                "queued_at": 900,
                "priority": "high",
                "priority_until": 2000,
            },
        }
        self.assertEqual(select_jobs(tasks, config, now=1000), [(1, "old")])
        tasks[(1, "old")]["retry_at"] = 2000
        self.assertEqual(select_jobs(tasks, config, now=1000), [(2, "new")])
