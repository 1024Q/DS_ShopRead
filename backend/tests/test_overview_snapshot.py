import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.business_reader import BEIJING
from backend.center.business import selected_node
from backend.center.overview_snapshot import (
    FIELDS,
    OverviewCache,
    install,
    project,
    sections,
    new_projection,
    attention_projection,
    trend_projection,
)
from unittest.mock import patch


NOW = datetime(2026, 10, 29, 12, tzinfo=BEIJING)


class FakeService:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.count_calls = []
        self.store = SimpleNamespace(
            list=lambda: [{"id": 1, "platform": "pdd", "name": "测试店铺", "username": "PRIVATE ACCOUNT"}]
        )

    def data(self, kind, options, **kwargs):
        self.calls.append((kind, options, kwargs))
        if self.fail:
            raise RuntimeError("PRIVATE DATABASE URL AND TOKEN")
        value = {
            "sources": [{"shop_id": 1, "updated_at": NOW.isoformat(), "stale": True}],
            "metrics": [
                {
                    "shop_id": 1,
                    "sales": 5,
                    "total_spend": 2,
                    "updated_at": NOW.isoformat(),
                    "rows": [{"order_number": "PRIVATE ORDER"}],
                }
            ],
            "rows": [{"password": "PRIVATE PASSWORD"}],
        }
        if kind == "products":
            value["rows"] = [
                {"shop_id": 1, "id": "private-product", "created_at": NOW.date().isoformat(), "password": "PRIVATE"}
            ]
        return value

    def home_counts(self, kind, options, **kwargs):
        self.count_calls.append((kind, options, kwargs))
        return {
            "sources": [
                {
                    "shop_id": 1,
                    "updated_at": NOW.isoformat(),
                    "complete": True,
                    "platform": "pdd",
                    "snapshot_scope": {**options, "token": "PRIVATE"},
                    "center_received_at": NOW.timestamp(),
                }
            ],
            "shop_counts": {1: 2},
            "rows": [],
        }


class OverviewSnapshotTests(unittest.TestCase):
    def test_projection_keeps_missing_values_and_only_allowed_aggregate_fields(self):
        shops = [
            {"id": 1, "platform": "pdd", "name": "公开店铺"},
            {"id": 2, "platform": "taobao"},
            {"id": 3, "platform": "PRIVATE PLATFORM"},
        ]
        overview = {
            "sources": [
                {"shop_id": 1, "updated_at": NOW.isoformat(), "center_received_at": NOW.timestamp(), "stale": True}
            ],
            "metrics": [
                {
                    "shop_id": 1,
                    "sales": 10.25,
                    "orders": 0,
                    "views": True,
                    "visitors": float("nan"),
                    "pending_shipping": 3,
                    "token": "PRIVATE",
                }
            ],
        }
        ads = {
            "metrics": [
                {"shop_id": 1, "total_spend": 4, "ad_spend": 100, "updated_at": NOW.isoformat()},
                {"shop_id": 2, "total_spend": 200, "ad_spend": 7, "updated_at": NOW.isoformat()},
                {"shop_id": 3, "ad_spend": 500, "updated_at": (NOW - timedelta(days=1)).isoformat()},
            ]
        }
        result = project(shops, overview, ads, NOW)
        self.assertEqual(set(result["totals"]), set(FIELDS))
        self.assertEqual(result["totals"]["sales"], 10.25)
        self.assertEqual(result["totals"]["orders"], 0)
        self.assertEqual(result["totals"]["ad_spend"], 11)
        self.assertIsNone(result["totals"]["views"])
        self.assertIsNone(result["totals"]["visitors"])
        self.assertEqual(result["coverage"]["ad_spend"], 2)
        self.assertEqual(result["shops"], {"total": 3, "read": 1, "stale": 1, "missing": 2})
        self.assertEqual(result["latest_received_at"], NOW.isoformat())
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertTrue(all(set(row) == {"platform", "shops", "read", "totals"} for row in result["platforms"]))

    def test_cache_expiry_and_beijing_day_rollover(self):
        service = FakeService()
        clock, day = [0], [NOW]
        cache = OverviewCache(service, clock=lambda: clock[0], now=lambda: day[0])
        first = cache.read()
        clock[0] = 14
        self.assertIs(cache.read(), first)
        self.assertEqual(len(service.calls), 5)
        clock[0] = 15
        cache.read()
        self.assertEqual(len(service.calls), 10)
        day[0] += timedelta(days=1)
        self.assertEqual(cache.read()["day"], "2026-10-30")
        self.assertEqual(len(service.calls), 15)
        self.assertTrue(all(kwargs.get("read_only") is True for _, _, kwargs in service.calls))

    def test_concurrent_requests_share_a_single_read(self):
        service = FakeService()
        original = service.data

        def slow(*args, **kwargs):
            time.sleep(0.01)
            return original(*args, **kwargs)

        service.data = slow
        cache = OverviewCache(service, now=lambda: NOW)
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: cache.read(), range(8)))
        self.assertEqual(len(service.calls), 5)
        self.assertTrue(all(r == results[0] for r in results))

    def test_failed_cache_refresh_does_not_return_old_values_as_fresh(self):
        service = FakeService()
        clock = [0]
        cache = OverviewCache(service, clock=lambda: clock[0], now=lambda: NOW)
        old = cache.read()
        clock[0] = 16
        service.fail = True
        with self.assertRaises(RuntimeError):
            cache.read()
        self.assertIs(cache.saved, old)
        service.fail = False
        cache.read()
        self.assertEqual(len(service.calls), 11)

    def test_sections_share_original_shapes_but_never_publish_raw_business_rows(self):
        service = FakeService()
        original = service.data

        def read(kind, options, **kwargs):
            value = original(kind, options, **kwargs)
            if kind == "transactions":
                value["metrics"][0].update(
                    summary={"sales": 25, "refund_amount": 3, "token": "PRIVATE"},
                    trend=[
                        {
                            "date": NOW.date().isoformat(),
                            "series": "今日累计",
                            "hour": 4,
                            "sales": 25,
                            "orders": 2,
                            "password": "PRIVATE",
                        },
                        {
                            "date": (NOW - timedelta(days=1)).date().isoformat(),
                            "series": "昨日累计",
                            "hour": 4,
                            "sales": 20,
                        },
                        {"date": "2020-01-01", "series": "昨日累计", "hour": 4, "sales": 100},
                        {"date": NOW.date().isoformat(), "series": "今日累计", "hour": 28, "sales": 100},
                    ],
                )
            if kind == "orders":
                value["metrics"][0].update(
                    refund_orders=2,
                    refund_amount=3,
                    refund_date=NOW.date().isoformat(),
                    refund_basis="order_created_success",
                    refund_complete=True,
                )
            return value

        service.data = read
        shops = service.store.list()
        with patch(
            "backend.center.overview_snapshot.attention_projection",
            return_value={"total": 7, "pending": 2, "stale": 1, "interval_seconds": 60},
        ):
            value = sections(service, shops, read("ad_reports", {}), NOW)
        self.assertEqual(len(value["trades"]["metrics"][0]["trend"]), 2)
        self.assertEqual(value["trades"]["metrics"][0]["summary"]["sales"], 25)
        self.assertEqual(value["new_products"]["total"], 1)
        self.assertEqual(value["attention"]["total"], 7)
        self.assertEqual(value["refunds"]["metrics"][0]["refund_amount"], 3)
        self.assertEqual(value["refunds"]["metrics"][0]["refund_basis"], "order_created_success")
        self.assertEqual(value["counts"]["pending_shipping"]["home_counts"]["rows"][0]["count"], 2)
        self.assertTrue(value["counts"]["shipping_due"]["home_counts"]["rows"][0]["verified"])
        self.assertEqual(len(service.count_calls), 5)
        self.assertTrue(all(call[2].get("read_only") is True for call in service.calls[1:]))
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertNotIn("private-product", json.dumps(value))
        self.assertEqual(
            set(value["trades"]["metrics"][0]),
            {
                "shop_id",
                "shop_name",
                "platform",
                "operator",
                "operators",
                "updated_at",
                "center_received_at",
                "source_time",
                "day_pending",
                "stale",
                "complete",
                "waiting_login",
                "syncing",
                "error",
                "summary",
                "trend",
            },
        )

    def test_optional_failure_does_not_remove_core_metrics_or_echo_errors(self):
        service = FakeService()
        original = service.data

        def read(kind, options, **kwargs):
            if kind == "products":
                raise RuntimeError("PRIVATE DATABASE URL")
            return original(kind, options, **kwargs)

        service.data = read
        value = OverviewCache(service, now=lambda: NOW).read()
        self.assertEqual(value["totals"]["sales"], 5)
        self.assertFalse(value["sections"]["new_products"]["available"])
        self.assertTrue(value["sections"]["trades"]["available"])
        self.assertNotIn("PRIVATE", json.dumps(value))

    def test_new_products_preserves_saved_creation_count_projection(self):
        shops = {"1": {"id": 1, "name": "测试店铺", "platform": "pdd"}}
        data = {
            "rows": [],
            "sources": [
                {
                    "shop_id": 1,
                    "updated_at": NOW.timestamp(),
                    "complete": True,
                    "_creation_counts": {"days": {"2026-10-29": 3, "2026-10-28": 2, "2026-09-01": 7}, "unknown": 0},
                }
            ],
        }
        value = new_projection(data, shops, NOW)
        self.assertEqual(value["total"], 3)
        self.assertEqual(value["previous_total"], 2)
        self.assertEqual(value["monthly"][9]["count"], 5)
        self.assertTrue(value["complete"])
        self.assertEqual(value["shops"][0]["days"], {"2026-10-28": 2, "2026-10-29": 3})

    def test_trade_hours_accept_saved_integer_text_without_coercing_invalid_values(self):
        today = NOW.date().isoformat()
        yesterday = (NOW - timedelta(days=1)).date().isoformat()
        points = [
            {"date": today, "series": "今日累计", "hour": "00", "sales": 10},
            {"date": today, "series": "今日累计", "hour": "01", "sales": 20},
            {"date": yesterday, "series": "昨日累计", "hour": "00", "sales": 5},
            {"date": yesterday, "series": "昨日累计", "hour": "01", "sales": 8},
            {"date": today, "series": "今日累计", "hour": 2, "sales": 30},
        ]
        for bad in ("1.5", "junk", "", "001", "24", "-1", "NaN", 1.5, float("nan"), True):
            points.append({"date": today, "series": "今日累计", "hour": bad, "sales": 999})
        result = trend_projection(
            {"metrics": [{"shop_id": 1, "trend": points}]}, {"1": {"id": 1, "platform": "pdd"}}, NOW
        )
        projected = result["metrics"][0]["trend"]
        self.assertEqual(
            [(p["series"], p["hour"], p["sales"]) for p in projected],
            [("今日累计", 0, 10), ("今日累计", 1, 20), ("昨日累计", 0, 5), ("昨日累计", 1, 8), ("今日累计", 2, 30)],
        )

    def test_attention_compact_counter_uses_original_priority_and_freshness(self):
        from contextlib import nullcontext

        records = [
            {
                "scope": "attention:evidence",
                "updated": NOW.timestamp(),
                "payload": {"rows": [{"id": "PRIVATE", "status": "待举证"}, {"id": "PRIVATE2", "status": "已举证"}]},
            }
        ]
        query = SimpleNamespace(mappings=lambda: records)
        service = SimpleNamespace(
            database=SimpleNamespace(
                engine=SimpleNamespace(connect=lambda: nullcontext(SimpleNamespace(execute=lambda _: query)))
            ),
            store=SimpleNamespace(
                list=lambda: [{"platform": "pdd", "node_id": "PRIVATE", "local_id": 1, "collector_online": True}]
            ),
        )
        with (
            patch("backend.center.freshness.configurations", return_value={}),
            patch("backend.center.freshness.describe", return_value={"overdue": False}),
        ):
            value = attention_projection(service)
        self.assertEqual(value, {"total": 1, "pending": 5, "stale": 5, "interval_seconds": 60})

    def client(self, service):
        app = FastAPI()
        install(app, service, lambda: None)

        def admin():
            raise HTTPException(401, "login required")

        @app.get("/api/shops", dependencies=[Depends(admin)])
        def private_shops():
            return service.store.list()

        # Same broad authenticated fallback as the center: snapshot methods must
        # be explicitly rejected, never dispatched as management commands.
        @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"], dependencies=[Depends(admin)])
        def fallback(path: str):
            return {}

        return TestClient(app)

    def test_authenticated_snapshot_rejects_commands(self):
        service = FakeService()
        with self.client(service) as client:
            response = client.get("/api/home/snapshot")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotIn("PRIVATE", response.text)
            self.assertEqual(client.get("/api/shops").status_code, 401)
            self.assertEqual(client.get("/api/home/snapshot?force=true").status_code, 400)
            for method in ("patch", "options", "head"):
                self.assertEqual(getattr(client, method)("/api/home/snapshot").status_code, 405)
            self.assertEqual(len(service.calls), 5)
            self.assertTrue(all(call[2].get("read_only") is True for call in service.calls))

    def test_snapshot_failure_hides_sensitive_exception(self):
        service = FakeService()
        service.fail = True
        with self.client(service) as client:
            response = client.get("/api/home/snapshot")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotIn("PRIVATE", response.text)

    def test_node_scoped_alias_cannot_poison_global_snapshot_cache(self):
        service = FakeService()
        app = FastAPI()
        install(app, service, lambda: None)
        endpoint = next(r.endpoint for r in app.routes if r.path == "/api/home/snapshot")
        from starlette.requests import Request

        token = selected_node.set("private-node")
        try:
            result = endpoint(
                Request(
                    {
                        "type": "http",
                        "method": "GET",
                        "path": "/api/home/snapshot",
                        "query_string": b"",
                        "headers": [],
                    }
                )
            )
            self.assertEqual(result.status_code, 404)
            self.assertEqual(service.calls, [])
        finally:
            selected_node.reset(token)


if __name__ == "__main__":
    unittest.main()
