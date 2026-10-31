import json, tempfile, unittest, time
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from backend.store import Store
from backend import attention, table_index, collection, incremental_collection
from backend.operations import decorate, collect_operations, normalize_operation
from backend.business import BusinessService, options_for
from backend.business_reader import fetch_list
from backend.priority_rules import LABELS, KINDS, VERSION


class PriorityRules(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("one", "pw")
        self.other = self.store.add("two", "pw", platform="taobao")
        self.shop = self.store.list()[0]
        self.service = BusinessService(self.store, lambda _: None)

    def payload(self, rows, started=100):
        return {"rows": rows, "complete": True, "_sync": {"snapshot_started_at": started}}

    def test_exact_states_and_platform_isolation(self):
        for kind, labels in LABELS.items():
            for label in labels:
                self.assertEqual(decorate(kind, {"status": label}, "pdd")["priority"], 3)
                self.assertEqual(decorate(kind, {"status": label}, "taobao")["priority"], 0)
                self.assertEqual(
                    decorate(kind, {"status": "已处理", "platform": "jd", "attention_status": label})["priority"], 0
                )
            self.assertFalse(decorate(kind, {"status": "非" + next(iter(labels))})["priority"])
        for kind, status in [
            ("evidence", "超时未举证"),
            ("tickets", "违规已处理"),
            ("tickets", "待处理"),
            ("tickets", "处理中"),
            ("aftersales", "待商家处理"),
            ("aftersales", "待商家举证"),
            ("invoices", "逾期未开票"),
            ("packages", "发货未揽件"),
        ]:
            self.assertFalse(decorate(kind, {"status": status})["priority"])
        self.assertEqual(list(attention.scopes({"platform": "taobao"})), [])
        self.assertEqual(set(dict(attention.scopes(self.shop))), KINDS)

    async def test_official_quick_filters_and_marker(self):
        calls = []

        async def page(session, kind, body, *args):
            calls.append((kind, body, args[-1]))
            return {"rows": [], "source_loaded": 0, "source_total": 0, "complete": True}

        with (
            patch("backend.operations.paginate", side_effect=page),
            patch("backend.operations.time.time", return_value=1700000000),
        ):
            for kind in ("packages", "invoices"):
                await collect_operations(SimpleNamespace(), kind, dict(attention.scopes(self.shop))[kind])
        package, invoice = calls
        self.assertEqual(
            {
                k: package[1][k]
                for k in ("packStatus", "exceptionStatus", "queryType", "shippingTimeStart", "shippingTimeEnd")
            },
            dict(
                packStatus=10, exceptionStatus=0, queryType=3, shippingTimeStart=1699913600, shippingTimeEnd=1699956800
            ),
        )
        self.assertEqual(invoice[1]["invoice_display_status"], 0)
        self.assertEqual(invoice[1]["promise_time_begin"], 1700000000)
        self.assertEqual(invoice[1]["promise_time_end"], 1700259200)
        for kind, item, label in [
            ("packages", {"orderSn": "o", "trackNo": "t", "packStatus": 10}, package[2]),
            ("invoices", {"order_sn": "i"}, invoice[2]),
        ]:
            row = normalize_operation(kind, item, label)[0]
            self.assertEqual(row["attention_status"], label)
            self.assertEqual(row["priority"], 3)
        session = SimpleNamespace(
            evaluate=AsyncMock(
                return_value=json.dumps(
                    {"success": True, "result": {"dataList": [{"instanceId": "x", "status": 0}], "total": 1}}
                )
            )
        )
        result = await fetch_list(session, "tickets", dict(attention.scopes(self.shop))["tickets"])
        self.assertEqual(session.evaluate.await_count, 1)
        self.assertIn('"serviceStatus": [4]', session.evaluate.call_args.args[0])
        self.assertEqual(result["rows"][0]["attention_status"], "2小时内将逾期")

    def test_priority_pinned_on_any_sort_and_foreign_shop_not_red(self):
        rows = [
            {"id": "normal", "status": "待商家处理", "amount": 999},
            {"id": "focus", "status": "平台处理中", "amount": 1},
        ]
        for sid in (self.sid, self.other):
            self.store.write_cache(sid, "aftersales:x", self.payload(rows))
        choices = [(s, "aftersales:x") for s in self.store.list()]
        result = table_index.query(self.store, choices, "aftersales", sort_by="amount", sort_direction="desc")
        self.assertEqual(result["priority_count"], 1)
        self.assertEqual(result["rows"][0]["id"], "focus")
        self.assertEqual(result["rows"][0]["platform"], "pdd")

    def test_filter_membership_clears_without_deleting_order(self):
        for kind, base, label in [
            ("tickets", "待处理", "2小时内将逾期"),
            ("invoices", "待开票", "3天内需开票"),
            ("packages", "发货未揽件", "即将发货-揽收超时"),
        ]:
            row = {"id": kind, "status": base}
            key = kind + ":x"
            self.store.write_cache(self.sid, key, self.payload([row]))
            attention.publish(self.store, self.sid, kind, self.payload([{**row, "attention_status": label}], 200))
            query = lambda: table_index.query(self.store, [(self.shop, key)], kind)
            self.assertEqual(query()["priority_count"], 1)
            # A later broad snapshot knows normal status, not quick-filter membership.
            self.store.write_cache(self.sid, key, self.payload([row], 250))
            self.assertEqual(query()["priority_count"], 1)
            attention.publish(self.store, self.sid, kind, self.payload([], 300))
            self.assertEqual(query()["priority_count"], 0)
            self.assertEqual(query()["rows"][0]["status"], base)
            self.store.write_cache(self.sid, key, self.payload([row], 400))
            self.assertEqual(query()["priority_count"], 0)

    def test_migration_recomputes_old_priority_and_attention_keyword(self):
        old = self.payload(
            [
                {"id": "old", "status": "超时未举证", "priority": 2},
                {"id": "keep", "status": "待举证", "order_number": "订单123", "priority": 3},
            ]
        )
        self.store.write_cache(self.sid, "evidence:x", old)
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO attention_snapshots VALUES(?,?,?,?,?)", (self.sid, "evidence", json.dumps(old), 100, 100)
            )
            db.execute("UPDATE table_snapshots SET metadata='{}'")
            db.execute("UPDATE table_rows SET priority=2")
        attention.initialize(self.store)
        table_index.initialize(self.store)
        result = attention.read(self.service, kind="evidence", keyword="订单123")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["rows"][0]["id"], "keep")
        self.assertEqual(attention.read(self.service, keyword="not-found")["total"], 0)
        self.assertEqual(table_index.query(self.store, [(self.shop, "evidence:x")], "evidence")["priority_count"], 1)
        self.assertGreater(result["pending"], 0)

    def test_normal_package_300s_focus_60s_and_no_delta(self):
        from backend.tests.collection_fixture import assign

        config = assign(self.store, {"packages@base": "custom", "packages@attention": "realtime"})
        self.assertEqual(collection.interval_seconds("packages", config), 300)
        options = dict(attention.scopes(self.shop))["packages"]
        self.assertFalse(collection.due("packages", options, 100, config, 159))
        self.assertTrue(collection.due("packages", options, 100, config, 160))
        self.assertFalse(incremental_collection.supported(SimpleNamespace(shop=self.shop), "packages", options))

    def test_new_focus_order_joins_table_before_full_collection_and_respects_filters(self):
        self.store.write_cache(self.sid, "evidence:x", self.payload([]))
        attention.publish(
            self.store,
            self.sid,
            "evidence",
            self.payload([{"id": "new", "status": "待举证", "created_at": "2026-10-10", "order_number": "123"}], 200),
        )
        query = lambda **kw: table_index.query(self.store, [(self.shop, "evidence:x")], "evidence", **kw)
        self.assertEqual(query()["rows"][0]["id"], "new")
        self.assertEqual(query(date_to="2026-10-09")["total"], 0)
        self.assertEqual(query(keyword="123", priority_only=True)["total"], 1)
        self.store.write_cache(self.sid, "evidence:x", self.payload([{"id": "new", "status": "待举证"}], 300))
        self.assertEqual(query()["total"], 1)

    def test_complete_focus_clears_stale_direct_status_priority_only_inside_coverage(self):
        rows = [
            {"id": "old", "status": "待举证", "created_at": "2026-10-10"},
            {"id": "outside", "status": "待举证", "created_at": "2026-01-01"},
        ]
        scope = {"date_from": "2026-04-14", "date_to": "2026-10-10"}
        self.store.write_cache(self.sid, "evidence:x", self.payload(rows))
        payload = self.payload([], 200)
        payload["_sync"]["scope"] = scope
        attention.publish(self.store, self.sid, "evidence", payload)
        query = lambda: table_index.query(self.store, [(self.shop, "evidence:x")], "evidence")
        actual = {r["id"]: r for r in query()["rows"]}
        self.assertEqual(actual["old"]["priority"], 0)
        self.assertEqual(actual["old"]["status"], "待举证")
        self.assertEqual(actual["outside"]["priority"], 3)
        self.store.write_cache(self.sid, "evidence:x", self.payload(rows, 150))
        self.assertEqual({r["id"]: r["priority"] for r in query()["rows"]}, {"old": 0, "outside": 3})
        payload = self.payload([rows[0]], 300)
        payload["_sync"]["scope"] = scope
        attention.publish(self.store, self.sid, "evidence", payload)
        self.assertEqual(query()["priority_count"], 2)

    def test_old_projection_platform_priority_is_rebuilt_even_when_cache_timestamp_matches(self):
        self.store.write_cache(self.other, "aftersales:x", self.payload([{"id": "foreign", "status": "平台处理中"}]))
        with self.store.connect() as db:
            db.execute(
                "UPDATE table_rows SET priority=2,payload=json_set(payload,'$.priority',2) WHERE shop_id=?",
                (self.other,),
            )
            db.execute(
                "UPDATE table_snapshots SET metadata=json_set(metadata,'$._priority_rules_version',2) WHERE shop_id=?",
                (self.other,),
            )
        table_index.initialize(self.store)
        foreign = next(s for s in self.store.list() if s["id"] == self.other)
        self.assertEqual(
            table_index.query(self.store, [(foreign, "aftersales:x")], "aftersales", priority_only=True)["total"], 0
        )
