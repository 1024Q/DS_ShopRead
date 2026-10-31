import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from backend import collection
from backend.business import BusinessService
from backend.collection_queue import select_jobs
from backend.frequency_policy import migrate
from backend.master_tasks import project, representatives, label


class TaskLayoutRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.config = migrate(copy.deepcopy(collection.DEFAULTS))

    def test_move_and_rename_only_changes_projection_not_running_identity_or_frequency(self):
        progress = {
            (1, "goodsdata:original"): {
                "kind": "goodsdata",
                "scope": {"trade_range": "realtime"},
                "phase": "running",
                "started_at": time.time() - 4,
                "platform": "pdd",
                "loaded": 20,
                "total": 40,
            }
        }
        deferred = {
            (1, "ad_products:original"): {
                "kind": "ad_products",
                "scope": {},
                "phase": "queued",
                "platform": "pdd",
                "last_attempt": 123,
                "continuation": True,
            }
        }
        service = SimpleNamespace(queue_config=self.config, progress=progress, deferred=deferred)
        before = project(service)
        original_progress = copy.deepcopy(progress)
        original_deferred = copy.deepcopy(deferred)
        changed = copy.deepcopy(self.config)
        changed["task_layout"]["groups"].append({"id": "custom_test", "name": "自定义商品任务"})
        changed["task_layout"]["assignments"]["pdd"]["goodsdata@base"] = "custom_test"
        service.queue_config = changed
        after = project(service)
        original_steps = {s["cache_key"]: s for row in before for s in row["steps"]}
        current_steps = {s["cache_key"]: s for row in after for s in row["steps"]}
        self.assertEqual(set(original_steps), set(current_steps))
        self.assertEqual(progress, original_progress)
        self.assertEqual(deferred, original_deferred)
        self.assertEqual(changed["task_modes"], self.config["task_modes"])
        self.assertEqual(len(after), 2)
        moved = next(row for row in after if row["master_id"] == "custom_test")
        self.assertEqual(moved["module"], "自定义商品任务")
        self.assertEqual(moved["phase"], "running")
        for key in original_steps:
            self.assertEqual(original_steps[key]["frequency"], current_steps[key]["frequency"])
        self.assertTrue(label("goodsdata", {}, changed).startswith("自定义商品任务 / "))
        runtime = BusinessService(None, None)
        runtime.queue_config = self.config
        runtime.progress = progress
        running = Mock()
        runtime.jobs = {(1, "goodsdata:original"): running}
        runtime.configure_queue(changed)
        running.cancel.assert_not_called()
        self.assertEqual(runtime.progress, original_progress)

    def test_different_parents_do_not_allow_same_business_to_run_twice(self):
        changed = copy.deepcopy(self.config)
        changed["task_layout"]["assignments"]["pdd"]["goodsdata@week"] = "home_metrics"
        shop = {"id": 1, "platform": "pdd"}
        candidates = [
            (shop, None, "goodsdata", {"trade_range": "realtime"}, {"last_attempt": 10}),
            (shop, None, "goodsdata", {"trade_range": "week"}, {"last_attempt": 5}),
            (shop, None, "overview", {}, {"last_attempt": 1}),
        ]
        self.assertEqual(
            representatives(candidates, {}, self.config, 100), representatives(candidates, {}, changed, 100)
        )
        chosen, deferred = representatives(candidates, {}, changed, 100)
        self.assertEqual(len(chosen), 2)
        self.assertEqual(len(deferred), 1)
        progress = {
            (1, str(index)): {"kind": kind, "scope": scope, "phase": "queued", "queued_at": 1}
            for index, (_, _, kind, scope, _) in enumerate(candidates)
        }
        selected = select_jobs(progress, {**changed, "max_concurrent": 20, "shop_concurrent": 3}, now=100)
        self.assertEqual(len(selected), 2)
        self.assertEqual(sum(progress[key]["kind"] == "goodsdata" for key in selected), 1)

    def test_home_pause_follows_step_not_parent_name(self):
        changed = copy.deepcopy(self.config)
        changed["home_paused"] = True
        changed["task_layout"]["assignments"]["pdd"]["overview@base"] = "daily_business"
        service = SimpleNamespace(
            queue_config=changed,
            progress={},
            deferred={},
            master_steps={(1, "overview:original"): {"kind": "overview", "scope": {}, "phase": "waiting"}},
        )
        self.assertEqual(project(service), [])


if __name__ == "__main__":
    unittest.main()
