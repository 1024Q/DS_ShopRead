import unittest
from unittest.mock import patch
from types import SimpleNamespace
from backend import order_plan, task_lifecycle


class RemovedAllOrdersTests(unittest.TestCase):
    def test_legacy_backfill_cannot_survive_even_as_subscription(self):
        for scope in ({}, {"_order_lane": "history"}, {"_order_lane": "daily"}):
            self.assertFalse(task_lifecycle.retained({"kind": "orders", "continuation": 1, "failures": 3}, scope, True))
        for scope in order_plan.live():
            self.assertFalse(order_plan.disabled_scope(scope))

    def test_empty_history_does_not_schedule_ninety_day_fill(self):
        with patch("backend.table_index.metadata", return_value=None):
            self.assertEqual(list(order_plan.background(None, {"id": 1})), [])

    def test_all_orders_request_rejected(self):
        with self.assertRaisesRegex(ValueError, "全部订单采集已停用"):
            order_plan.request(None, {"status": ""}, None, True, None)

    def test_status_refresh_does_not_create_history_tasks(self):
        scopes = []
        service = SimpleNamespace(store=SimpleNamespace(list=lambda: [{"id": 1, "platform": "pdd"}]))

        def request(kind, scope, *args, **kwargs):
            scopes.append(scope)
            return {"tasks": []}

        service.request_sync = request
        order_plan.request(
            service, {"status": "待发货", "date_from": "2026-01-01", "date_to": "2026-04-01"}, None, True, None
        )
        self.assertTrue(scopes)
        self.assertFalse(any(order_plan.disabled_scope(s) for s in scopes))
        self.assertEqual([s["status"] for s in scopes if s.get("_order_lane") == "open"], ["待发货"])
