"""Queue sequence numbers and wait estimates follow the admission limits."""

import copy
import time
import unittest
from types import SimpleNamespace

from backend import collection
from backend.frequency_policy import migrate
from backend.queue_eta import describe, estimates, record


def service(**limits):
    config = migrate(copy.deepcopy(collection.DEFAULTS))
    config.update(limits)
    return SimpleNamespace(queue_config=config, runtime_limits={}, progress={}, step_durations={})


class QueueEtaTests(unittest.TestCase):
    def test_positions_and_waits_respect_capacity(self):
        s = service(max_concurrent=1, max_active_shops=2, shop_concurrent=1)
        now = time.time()
        s.progress[(1, "overview:a")] = {"kind": "overview", "phase": "running", "scope": {}, "started_at": now - 10}
        for n, kind in enumerate(("products", "reviews", "tickets")):
            s.progress[(2, kind + ":x")] = {"kind": kind, "phase": "queued", "scope": {}, "queued_at": now - 30 + n}
        for kind in ("overview", "products", "reviews", "tickets"):
            record(s, 1, kind, {}, 40)
            record(s, 2, kind, {}, 40)
        result = estimates(s, now)
        self.assertEqual(sorted(v["queue_position"] for v in result.values()), [1, 2, 3])
        self.assertTrue(all(v["queue_total"] == 3 for v in result.values()))
        waits = sorted(v["wait_seconds"] for v in result.values())
        # One slot: first waits for the running read (~30s left), then 40s per step.
        self.assertEqual(waits, [30, 70, 110])

    def test_same_shop_module_and_shop_limit_serialise(self):
        s = service(max_concurrent=4, max_active_shops=1, shop_concurrent=2)
        now = time.time()
        s.progress[(1, "reviews:a")] = {
            "kind": "reviews",
            "phase": "queued",
            "scope": {"stars": [5]},
            "queued_at": now - 5,
        }
        s.progress[(1, "reviews:b")] = {
            "kind": "reviews",
            "phase": "queued",
            "scope": {"stars": [4]},
            "queued_at": now - 4,
        }
        s.progress[(2, "tickets:c")] = {"kind": "tickets", "phase": "queued", "scope": {}, "queued_at": now - 3}
        record(s, 1, "reviews", {"stars": [5]}, 20)
        record(s, 1, "reviews", {"stars": [4]}, 20)
        record(s, 2, "tickets", {}, 20)
        result = estimates(s, now)
        self.assertEqual(result[(1, "reviews:a")]["wait_seconds"], 0)
        # Same shop + module never run in parallel; only one active shop allowed.
        self.assertGreaterEqual(result[(1, "reviews:b")]["wait_seconds"], 20)
        self.assertGreaterEqual(result[(2, "tickets:c")]["wait_seconds"], 20)

    def test_unknown_duration_falls_back_and_text(self):
        s = service(max_concurrent=1)
        s.progress[(1, "products:x")] = {"kind": "products", "phase": "queued", "scope": {}, "queued_at": time.time()}
        value = estimates(s)[(1, "products:x")]
        self.assertEqual(value["queue_position"], 1)
        self.assertEqual(value["wait_seconds"], 0)
        self.assertGreater(value["expected_seconds"], 0)
        self.assertEqual(describe(None), "暂无法估算")
        self.assertEqual(describe(2), "即将开始")
        self.assertEqual(describe(45), "约 45 秒")
        self.assertEqual(describe(125), "约 2 分 5 秒")
        self.assertEqual(describe(3 * 3600 + 120), "约 3 小时 2 分钟")


if __name__ == "__main__":
    unittest.main()


class DurationPersistenceTests(unittest.TestCase):
    def test_durations_survive_restart(self):
        import tempfile
        from pathlib import Path
        from backend.queue_eta import durations, save

        with tempfile.TemporaryDirectory() as folder:
            first = SimpleNamespace(store=SimpleNamespace(directory=Path(folder)))
            record(first, 3, "products", {}, 42)
            save(first, force=True)
            second = SimpleNamespace(store=SimpleNamespace(directory=Path(folder)))
            self.assertEqual(durations(second), {(3, "products@base"): 42.0})
