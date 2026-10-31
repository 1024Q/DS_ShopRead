import json
import tempfile
import unittest
from unittest.mock import patch
from backend.store import Store
from backend import table_index, collection
from backend.business import filter_rows
from backend.operations import decorate
from backend.table_sort import sort_rows


class TableIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("fixture", "password", name="同名店铺")
        self.other = self.store.add("fixture2", "password", name="同名店铺", platform="taobao")
        self.shop = self.store.list()[0]
        self.rows = [
            {
                "id": str(100000000000000000 + i),
                "name": f"商品{i:04d}",
                "goods_id": str(i),
                "status": ["待商家处理", "平台处理中", "平台介入待商家处理"][i % 3],
                "created_at": f"2026-10-{i % 9 + 1:02d} 12:00:00",
                "deadline": f"2026-10-{i % 9 + 1:02d}",
                "amount": None if i % 17 == 0 else i % 37 + 0.23,
                "stock": i % 8,
                "price_min": i % 11,
                "price_max": i % 11 + 3,
                "stars": i % 5 + 1,
                "replied": bool(i % 2),
                "carrier": "SF快递" if i % 2 else "中通",
                "activity_type": "大促" if i % 2 else "补贴",
                "code": "%_测试" if i % 7 == 0 else "",
                "thumbnail_url": "https://example.com/product.png",
            }
            for i in range(231)
        ]
        self.store.write_cache(self.sid, "aftersales:fixture", {"rows": self.rows, "complete": True})

    def query(self, kind="aftersales", **kwargs):
        return table_index.query(self.store, [(self.shop, "aftersales:fixture")], kind, **kwargs)

    def test_quality_migration_and_priority_filter_exclude_inactive_records(self):
        key = "quality:fixture"
        rows = [
            {"id": "active", "status": "品质违规商品", "effect_status": "生效中"},
            {"id": "inactive", "status": "品质违规商品", "effect_status": "非生效"},
            {"id": "unknown", "status": "预警商品"},
        ]
        self.store.write_cache(self.sid, key, {"rows": rows, "complete": True})
        with self.store.connect() as db:
            db.execute(
                "UPDATE table_rows SET priority=1,payload=json_set(payload,'$.priority',1) WHERE cache_key=?", (key,)
            )
            db.execute(
                "UPDATE table_snapshots SET metadata=json_remove(metadata,'$._quality_rules_version') WHERE cache_key=?",
                (key,),
            )
        table_index.initialize(self.store)
        query = lambda **kw: table_index.query(self.store, [(self.shop, key)], "quality", **kw)
        self.assertEqual([r["id"] for r in query(priority_only=True)["rows"]], ["active"])
        self.assertEqual(query()["total"], 3)
        self.assertEqual(query()["priority_count"], 1)

    def test_package_deadlines_backfill_sort_and_export_existing_records(self):
        key = "packages:fixture"
        self.store.write_cache(
            self.sid,
            key,
            {
                "rows": [
                    {"id": "later", "status": "发货未揽件", "created_at": "2026-10-11 14:00:00"},
                    {"id": "first", "status": "发货未揽件", "created_at": "2026-10-11 10:00:00"},
                    {"id": "done", "status": "已签收", "created_at": "2026-10-10 10:00:00"},
                ]
            },
        )
        with self.store.connect() as db:
            db.execute(
                "UPDATE table_rows SET payload=json_remove(payload,'$.pickup_deadline') WHERE cache_key=?", (key,)
            )
            db.execute(
                "UPDATE table_snapshots SET metadata=json_remove(metadata,'$._package_timing_version') WHERE cache_key=?",
                (key,),
            )
        table_index.initialize(self.store)
        result = table_index.query(
            self.store, [(self.shop, key)], "packages", sort_by="pickup_deadline", sort_direction="asc", export=True
        )
        self.assertEqual([r["id"] for r in result["rows"]], ["first", "later", "done"])
        self.assertEqual(result["rows"][0]["pickup_deadline"], "2026-10-12T10:00:00+08:00")
        self.assertIsInstance(result["rows"][0]["pickup_hours_remaining"], float)
        self.assertIsNone(result["rows"][2]["pickup_hours_remaining"])

    def legacy(self, kind="aftersales", sort_by="", sort_direction="desc", **filters):
        rows = filter_rows([decorate("aftersales", r) for r in self.rows], kind, **filters)
        rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        rows.sort(
            key=lambda r: (
                (-r["priority"], r.get("deadline") or "9999") if r["priority"] > 1 else (-r["priority"], "9999")
            )
        )
        rows = sort_rows(rows, sort_by, sort_direction)
        from backend.priority_rules import KINDS

        if kind in KINDS:
            rows.sort(key=lambda r: not bool(r.get("priority")))
        return rows

    def test_filters_sort_and_export_match_previous_semantics(self):
        cases = [
            {},
            dict(keyword="%_测试"),
            dict(keyword="商品001"),
            dict(status="平台处理中"),
            dict(priority_only=True),
            dict(carrier="sf"),
            dict(date_from="2026-10-02", date_to="2026-10-05"),
            dict(min_price=4, max_price=9, low_stock=3),
            dict(kind="reviews", stars=[1, 2, 3], reply="unreplied"),
            dict(kind="activities", activity_type="大促"),
        ]
        for case in cases:
            for field in ("", "amount", "name", "created_at", "id"):
                for direction in ("asc", "desc"):
                    args = {**case, "sort_by": field, "sort_direction": direction}
                    expected = self.legacy(**args)
                    actual = self.query(**args, export=True)
                    self.assertEqual([r["id"] for r in actual["rows"]], [r["id"] for r in expected], args)
                    self.assertEqual(actual["total"], len(expected))
                    self.assertEqual(actual["priority_count"], sum(bool(r["priority"]) for r in expected))
                    page = self.query(**args, page=2, page_size=20)
                    offset = (min(2, max(1, (len(expected) + 19) // 20)) - 1) * 20
                    self.assertEqual([r["id"] for r in page["rows"]], [r["id"] for r in expected[offset : offset + 20]])

    def test_scopes_identity_and_duplicate_names(self):
        self.store.write_cache(self.other, "aftersales:fixture", {"rows": [self.rows[0]]})
        self.store.write_cache(self.sid, "aftersales:different", {"rows": [dict(self.rows[0], name="另一个日期")]})
        result = table_index.query(
            self.store, [(s, "aftersales:fixture") for s in self.store.list()], "aftersales", keyword="商品0000"
        )
        self.assertEqual(result["total"], 2)
        self.assertEqual({r["platform"] for r in result["rows"]}, {"pdd", "taobao"})
        self.store.assign_operator([self.sid], "新运营")
        self.shop = self.store.list()[0]
        self.assertEqual(self.query(keyword="商品0000")["rows"][0]["operator"], "新运营")

    def test_thumbnail_and_fallback_name_filter(self):
        self.store.write_cache(self.sid, "products:fixture", {"rows": [dict(self.rows[0], name="补全名称")]})
        self.store.write_cache(
            self.sid, "aftersales:fixture", {"rows": [dict(self.rows[0], name="", thumbnail_url="")]}
        )
        result = self.query(keyword="补全名称")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["rows"][0]["name"], "补全名称")
        self.assertEqual(result["rows"][0]["thumbnail_url"], "https://example.com/product.png")

    def test_atomic_replace_and_delete(self):
        with patch("backend.table_index.write", side_effect=RuntimeError("failed projection")):
            with self.assertRaises(RuntimeError):
                self.store.write_cache(self.sid, "aftersales:fixture", {"rows": []})
        self.assertEqual(self.query()["total"], 231)
        self.assertEqual(len(self.store.read_cache(self.sid, "aftersales:fixture")["payload"]["rows"]), 231)
        self.store.write_cache(self.sid, "aftersales:fixture", {"rows": []})
        self.assertEqual(self.query()["total"], 0)
        self.store.delete(self.sid)
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM table_snapshots WHERE shop_id=?", (self.sid,)).fetchone()[0], 0
            )

    def test_old_database_backfill_and_archive_survives_eviction(self):
        key = "products:archived"
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO daily_archive VALUES(?,?,?,?)", (self.sid, key, json.dumps({"rows": self.rows[:3]}), 1)
            )
            db.execute("DELETE FROM table_rows")
            db.execute("DELETE FROM table_snapshots")
        table_index.initialize(self.store)
        self.assertEqual(self.query()["total"], 231)
        from backend.store import CACHE_LIMIT

        for i in range(CACHE_LIMIT + 2):
            self.store.write_cache(self.sid, f"products:{i}", {"rows": []})
        self.store._cache_pruned.clear()
        self.store._prune_cache(self.sid)
        self.assertIsNotNone(table_index.metadata(self.store, self.sid, key))
        self.assertIsNone(table_index.metadata(self.store, self.sid, "aftersales:fixture"))
        self.assertEqual(table_index.query(self.store, [(self.shop, key)], "products")["total"], 3)

    def test_only_requested_page_materialized_no_snapshot_scan(self):
        with (
            patch.object(self.store, "read_cache", side_effect=AssertionError("full snapshot read")),
            patch.object(self.store, "product_images", side_effect=AssertionError("unneeded image scan")),
        ):
            self.assertEqual(len(self.query(page=3, page_size=20)["rows"]), 20)

    def test_invalid_sort_is_rejected(self):
        with self.assertRaises(ValueError):
            self.query(sort_by="name'); DROP TABLE shops;--")
