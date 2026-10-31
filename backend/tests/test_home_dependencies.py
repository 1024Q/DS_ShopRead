import copy
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from backend import attention, collection, home_progress
from backend.business import BusinessService, options_for
from backend.business_reader import BEIJING
from backend.center.coverage import project
from backend.frequency_policy import task_id
from backend.home_dependencies import scopes
from backend.scheduler import Scheduler
from backend.store import Store
from backend.task_catalog import HOME


class HomeDependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.sid = self.store.add("coverage-fixture", "test")
        self.shop = self.store.list()[0]
        self.item = SimpleNamespace(state="logged_in")
        self.service = BusinessService(self.store, lambda _: self.item)
        self.entries = {identifier: (kind, options) for identifier, kind, options, _ in scopes(self.shop)}

    def tearDown(self):
        self.temp.cleanup()

    def save(self, identifier, payload=None):
        kind, options = self.entries[identifier]
        value = {"rows": [], "complete": True, "_sync": {"snapshot_started_at": time.time()}}
        if identifier == "orders@refund":
            value["metrics"] = {
                "refund_complete": True,
                "refund_date": options["date_from"],
                "refund_basis": "order_created_success",
                "refund_orders": 0,
                "refund_amount": 0,
            }
        value.update(payload or {})
        if options.get("_attention"):
            attention.publish(self.store, self.sid, kind, value)
        else:
            self.store.write_cache(self.sid, self.service.key(kind, options, self.shop), value)
        return value

    def result(self):
        data = home_progress.read(self.service)
        return data, {m["task_id"]: m for m in data["rows"][0]["modules"]}

    def test_coverage_matches_home_catalog_and_scheduled_cache_identities(self):
        self.assertEqual(set(self.entries), HOME)
        scheduled = list(Scheduler(self.service).scopes()) + list(attention.scopes(self.shop))
        scheduled.append(("aftersales", options_for("aftersales", status="待处理即将逾期")))
        keys = {task_id(k, o): self.service.key(k, o, self.shop) for k, o in scheduled}
        for identifier, (kind, options) in self.entries.items():
            self.assertEqual(self.service.key(kind, options, self.shop), keys[identifier])
        self.assertEqual(
            {i for i, *_ in scopes({"platform": "taobao"})}, {"overview@base", "transactions@base", "ad_reports@base"}
        )
        self.assertEqual(list(scopes({"platform": "jd"})), [])

    def test_previous_hundred_percent_requires_all_three_missing_scopes(self):
        new = {"orders@refund", "orders@shipping_due", "aftersales@expiring"}
        for identifier in HOME - new:
            self.save(identifier)
        data, modules = self.result()
        self.assertEqual((data["updated_modules"], data["total_modules"]), (9, 12))
        self.assertEqual((data["daily_updated"], data["daily_total"]), (1, 1))
        self.assertEqual(len(modules), 13)
        self.assertFalse(any(modules[i]["today_updated"] for i in new))
        for index, identifier in enumerate(sorted(new), 1):
            self.save(identifier)
            data, modules = self.result()
            self.assertEqual(data["updated_modules"], 9 + index)
            self.assertTrue(modules[identifier]["today_updated"])
        self.assertEqual(data["updated_shops"], 1)

    def test_scope_completeness_and_original_source_clock_are_independent(self):
        self.save("aftersales@attention")
        self.save("orders@refund")
        self.save("orders@shipping_due", {"complete": False})
        yesterday = (datetime.now(BEIJING) - timedelta(days=1)).timestamp()
        self.save("aftersales@expiring", {"_sync": {"snapshot_started_at": yesterday}})
        _, modules = self.result()
        self.assertTrue(modules["aftersales@attention"]["today_updated"])
        self.assertTrue(modules["orders@refund"]["today_updated"])
        self.assertFalse(modules["orders@shipping_due"]["today_updated"])
        self.assertFalse(modules["aftersales@expiring"]["today_updated"])
        # A successful list read is insufficient when actual refund amounts are not verified.
        valid = self.save("orders@refund")
        for changes in ({"refund_complete": False}, {"refund_date": "2000-01-01"}, {"refund_basis": "refund_time"}):
            self.save("orders@refund", {"metrics": {**valid["metrics"], **changes}})
            self.assertFalse(self.result()[1]["orders@refund"]["today_updated"])
        self.save("orders@refund", {"_sync": {"snapshot_started_at": yesterday}})
        self.assertFalse(self.result()[1]["orders@refund"]["today_updated"])

    def test_scope_reads_work_with_shop_databases_and_keep_configuration_unchanged(self):
        from unittest.mock import patch
        from backend.shard_connection import FederatedConnection

        before = copy.deepcopy(collection.settings(self.store))
        self.save("orders@refund")
        self.save("aftersales@expiring")
        # Both storage layouts must be compared at the same instant. Migration
        # may cross a second on a busy disk; relative ages are not data changes.
        moment = datetime.now(BEIJING)
        expected = home_progress.read(self.service, now=moment)
        self.store.enable_shop_databases()
        with patch.object(FederatedConnection, "read_all", side_effect=AssertionError("cross-shop read")):
            actual = home_progress.read(self.service, now=moment)
        self.assertEqual(actual, expected)
        self.assertEqual(collection.settings(self.store), before)
        self.assertFalse(self.service.jobs)
        self.item.state = "sleeping"
        self.assertEqual(self.result()[0]["total_modules"], 12)
        collection.save_settings(self.store, {"settings_patch": {"home_paused": True}})
        self.assertEqual(self.result()[0]["total_modules"], 0)

    def test_center_ownership_projection_preserves_distinct_scopes_and_clocks(self):
        self.save("orders@refund")
        self.save("aftersales@attention")
        value, _ = self.result()
        shops = [{"id": 42, "local_id": self.sid, "node_id": "node-a", "platform": "pdd", "name": "fixture"}]
        result = project(value, shops, "node-a", {})
        self.assertEqual(result["rows"][0]["shop_id"], 42)
        self.assertEqual(result["rows"][0]["modules"], value["rows"][0]["modules"])
        self.assertEqual((result["updated_modules"], result["total_modules"]), (2, 12))
        self.assertEqual(result["last_updated"], value["last_updated"])

    def test_injected_day_selects_that_days_scopes_not_the_system_day(self):
        at = (datetime.now(BEIJING) - timedelta(days=1)).replace(hour=23, minute=59, second=59)
        for identifier, kind, options, _ in scopes(self.shop, at=at):
            if identifier not in {"orders@refund", "orders@shipping_due", "transactions@base", "ad_reports@base"}:
                continue
            self.assertEqual(options.get("day", options.get("date_to")), at.date().isoformat())
            if "end" in options:
                self.assertEqual(datetime.fromtimestamp(options["end"], BEIJING).date(), at.date())
            payload = {"rows": [], "complete": True, "_sync": {"snapshot_started_at": at.timestamp()}}
            if identifier == "orders@refund":
                payload["metrics"] = {
                    "refund_complete": True,
                    "refund_date": at.date().isoformat(),
                    "refund_basis": "order_created_success",
                    "refund_orders": 0,
                    "refund_amount": 0,
                }
            self.store.write_cache(self.sid, self.service.key(kind, options, self.shop), payload)
        result = home_progress.read(self.service, now=at)
        self.assertEqual(result["date"], at.date().isoformat())
        self.assertEqual(result["updated_modules"], 4)
        self.assertEqual(self.result()[0]["updated_modules"], 0)


if __name__ == "__main__":
    unittest.main()
