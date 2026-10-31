import json, sqlite3, tempfile, threading, time, unittest
from unittest.mock import patch
from backend.store import Store, CACHE_LIMIT
from backend import table_index, collection, attention
from backend.business import image_catalog, scheduling_states
from backend.product_images import enrich

IMG = "https://img.pddpic.com/{}.jpg"


class DataLayerFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("fixture", "password", name="店铺甲", operator="运营甲,运营乙")
        self.shop = self.store.list()[0]

    def keys(self):
        with self.store.connect() as db:
            return {r[0] for r in db.execute("SELECT cache_key FROM business_cache WHERE shop_id=?", (self.sid,))}

    def test_updated_is_stamped_after_write_lock(self):
        blocker = sqlite3.connect(self.store.path, timeout=30, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        worker = threading.Thread(target=self.store.write_cache, args=(self.sid, "products:locked", {"rows": []}))
        worker.start()
        time.sleep(0.4)
        released = time.time()
        blocker.execute("COMMIT")
        blocker.close()
        worker.join(30)
        self.assertGreaterEqual(self.store.cache_info(self.sid, "products:locked")["updated"], released)

    def test_live_scheduled_scopes_survive_eviction(self):
        live = [f"orders:live{i}" for i in range(40)]
        for key in live:
            collection.register(self.store, self.sid, key, "orders", {}, None, False)
            self.store.write_cache(self.sid, key, {"rows": []})
        with self.store.connect() as db:
            db.execute("UPDATE collection_tasks SET schedule_state='periodic' WHERE shop_id=?", (self.sid,))
        self.store.write_cache(self.sid, "tax:2026-09", {"rows": []})
        for i in range(CACHE_LIMIT + 5):
            self.store.write_cache(self.sid, f"products:{i}", {"rows": []})
        self.store._cache_pruned.clear()
        self.store._prune_cache(self.sid)
        keys = self.keys()
        self.assertTrue(set(live) <= keys)
        self.assertIn("tax:2026-09", keys)
        self.assertEqual(len([k for k in keys if k.startswith("products:")]), CACHE_LIMIT)
        self.assertNotIn("products:0", keys)
        self.assertIn(f"products:{CACHE_LIMIT + 4}", keys)
        with self.store.connect() as db:
            db.execute("UPDATE collection_tasks SET schedule_state='archived' WHERE shop_id=?", (self.sid,))
        self.store.write_cache(self.sid, "products:last", {"rows": []})
        for _ in range(3):
            self.store._cache_pruned.clear()
            self.store._prune_cache(self.sid)
        self.assertFalse(set(live) & self.keys())

    def test_inserts_tolerate_additive_columns(self):
        with self.store.connect() as db:
            for table in (
                "business_cache",
                "table_snapshots",
                "attention_snapshots",
                "attention_rows",
                "attention_counts",
                "window_preferences",
                "operators",
            ):
                db.execute(f"ALTER TABLE {table} ADD COLUMN future_extra TEXT")
        self.store.write_cache(self.sid, "products:x", {"rows": [{"id": "1", "goods_id": "1", "name": "a"}]})
        self.assertEqual(self.store.read_cache(self.sid, "products:x")["payload"]["rows"][0]["id"], "1")
        attention.publish(
            self.store,
            self.sid,
            "tickets",
            {"rows": [{"id": "t", "status": "待处理", "attention_status": "2小时内将逾期"}], "complete": True},
        )
        self.store.set_window_preference(self.sid, True)
        self.store.save_operators(["新人"])

    def full_catalog(self):
        return self.store.product_images(self.sid)

    def test_goods_id_catalog_matches_full_scan_without_payload_decoding(self):
        self.store.write_cache(
            self.sid,
            "products:a",
            {
                "rows": [
                    {"id": "p1", "goods_id": "1", "name": "旧名", "thumbnail_url": IMG.format("old")},
                    {"id": "p2", "goods_id": 2, "name": "二", "hd_thumb_url": IMG.format("two")},
                    {"id": "p3", "goods_id": "3", "name": "无图"},
                ]
            },
        )
        self.store.write_cache(
            self.sid,
            "orders:b",
            {
                "rows": [
                    {"id": "o1", "goods_id": "1", "name": "新名", "thumbUrl": IMG.format("new")},
                    {"id": "o1b", "goods_id": "1", "name": "次", "thumbUrl": IMG.format("second")},
                    {"id": "o2", "goods_id": "3"},
                ]
            },
        )
        self.store.write_cache(
            self.sid, "overview:c", {"rows": [{"id": "v", "goods_id": "4", "thumbnail_url": IMG.format("ignored")}]}
        )
        full = self.full_catalog()
        with patch("backend.store.json.loads", side_effect=AssertionError("payload decoded")):
            indexed = self.store.product_images(self.sid, ["1", 2, "3", "4", "missing"])
        self.assertEqual(indexed, {k: v for k, v in full.items() if k in ("1", "2", "3", "4")})
        self.assertEqual(indexed["1"], {"thumbnail_url": IMG.format("new"), "name": "新名"})
        rows = [
            {"goods_id": "1"},
            {"goods_id": 2, "name": "自带"},
            {"goods_id": "3", "thumbnail_url": IMG.format("own"), "name": "x"},
            {"id": "none"},
        ]
        self.assertEqual(enrich(rows, image_catalog(self.store, self.sid, rows)), enrich(rows, full))

    def test_scheduling_states_matches_individual_reads(self):
        self.store.write_cache(self.sid, "products:a", {"rows": []})
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO daily_archive(shop_id,cache_key,payload,updated) VALUES(?,?,?,?)",
                (self.sid, "goodsdata:d", "{}", 5),
            )
        collection.register(
            self.store, self.sid, "products:a", "products", {}, self.store.cache_info(self.sid, "products:a")
        )
        pairs = [(self.sid, "products:a"), (self.sid, "goodsdata:d"), (self.sid, "none:x"), (999, "products:a")]
        caches, tasks = scheduling_states(self.store, pairs)
        for pair in pairs:
            self.assertEqual(caches.get(pair), self.store.cache_info(*pair))
            self.assertEqual(tasks.get(pair), collection.state(self.store, *pair))

    def test_startup_backfill_reads_payload_only_for_stale_snapshots(self):
        self.store.write_cache(self.sid, "products:a", {"rows": [{"id": "1", "goods_id": "1"}]})
        self.store.write_cache(self.sid, "reviews:b", {"rows": [{"id": "2"}]})
        with self.store.connect() as db:
            db.execute("UPDATE table_snapshots SET updated=1 WHERE cache_key='reviews:b'")
        statements = []
        original = self.store.connect
        from contextlib import contextmanager

        @contextmanager
        def traced():
            with original() as db:
                db.set_trace_callback(statements.append)
                yield db

        with patch.object(self.store, "connect", traced):
            table_index.initialize(self.store)
        payload_reads = [
            s
            for s in statements
            if "payload" in s
            and ("FROM business_cache" in s or "FROM daily_archive" in s)
            and s.lstrip().upper().startswith(("SELECT", "WITH"))
        ]
        self.assertEqual(len(payload_reads), 1)
        self.assertIn("reviews:b", payload_reads[0])
        self.assertFalse(any("c.*" in s for s in statements))

    def name_query(self, selections, **kw):
        return table_index.query(self.store, selections, "orders", **kw)

    def correlated_names(self):
        sql = """SELECT r.cache_key,r.ordinal,coalesce(nullif(r.name,''),(SELECT p.name FROM table_rows p JOIN table_snapshots t ON p.shop_id=t.shop_id AND p.cache_key=t.cache_key WHERE p.shop_id=r.shop_id AND p.goods_id=r.goods_id AND p.goods_id<>'' AND p.thumbnail_url<>'' ORDER BY t.updated DESC,p.ordinal LIMIT 1),'') FROM table_rows r"""
        new = "SELECT r.cache_key,r.ordinal,coalesce(nullif(r.name,''),(SELECT n.name FROM table_names n WHERE n.shop_id=r.shop_id AND n.goods_id=r.goods_id),'') FROM table_rows r"
        with self.store.connect() as db:
            return sorted(map(tuple, db.execute(sql))), sorted(map(tuple, db.execute(new)))

    def test_name_fallback_is_materialized_and_tracks_changes(self):
        orders = [
            {"id": "o1", "goods_id": "1", "status": "待发货", "created_at": "2026-10-01"},
            {"id": "o2", "goods_id": "2", "status": "待发货", "created_at": "2026-10-02"},
            {"id": "o3", "goods_id": "", "status": "待发货", "created_at": "2026-10-03"},
        ]
        self.store.write_cache(self.sid, "orders:k", {"rows": orders, "complete": True})
        sel = [(self.shop, "orders:k")]
        self.assertEqual(self.name_query(sel, keyword="蓝色")["total"], 0)
        self.store.write_cache(
            self.sid,
            "products:p",
            {
                "rows": [
                    {"id": "g1", "goods_id": "1", "name": "蓝色杯子", "thumbnail_url": IMG.format(1)},
                    {"id": "g2", "goods_id": "2", "name": "红色盘子", "thumbnail_url": IMG.format(2)},
                ]
            },
        )
        self.assertEqual([r["id"] for r in self.name_query(sel, keyword="蓝色")["rows"]], ["o1"])
        self.assertEqual(
            [r["id"] for r in self.name_query(sel, sort_by="name", sort_direction="asc")["rows"]][:2], ["o2", "o1"]
        )
        old, new = self.correlated_names()
        self.assertEqual(old, new)
        self.store.write_cache(
            self.sid,
            "products:p",
            {"rows": [{"id": "g1", "goods_id": "1", "name": "绿色杯子", "thumbnail_url": IMG.format(1)}]},
        )
        self.assertEqual(self.name_query(sel, keyword="蓝色")["total"], 0)
        self.assertEqual(self.name_query(sel, keyword="绿色")["total"], 1)
        self.assertEqual(self.name_query(sel, keyword="红色")["total"], 0)
        old, new = self.correlated_names()
        self.assertEqual(old, new)
        self.assertEqual(self.name_query(sel, keyword="运营乙")["total"], 3)

    def test_name_catalog_catches_up_after_older_release_writes(self):
        self.store.write_cache(self.sid, "orders:k", {"rows": [{"id": "o1", "goods_id": "1"}]})
        self.store.write_cache(
            self.sid,
            "products:p",
            {"rows": [{"id": "g1", "goods_id": "1", "name": "旧", "thumbnail_url": IMG.format(1)}]},
        )
        with self.store.connect() as db:
            # Simulate an older release rewriting a snapshot without maintaining table_names.
            db.execute("UPDATE table_rows SET name='新' WHERE cache_key='products:p'")
            db.execute("UPDATE table_snapshots SET updated=updated+100 WHERE cache_key='products:p'")
        table_index.initialize(self.store)
        old, new = self.correlated_names()
        self.assertEqual(old, new)
        self.store.write_cache(self.sid, "reviews:r", {"rows": [{"id": "r1", "goods_id": "7"}]})
        with self.store.connect() as db:
            # First upgrade: no watermark yet, while the backfill also rewrites an unrelated stale snapshot.
            db.execute("DELETE FROM settings WHERE key='table_names_watermark'")
            db.execute("DELETE FROM table_names")
            db.execute("UPDATE table_snapshots SET updated=1 WHERE cache_key='reviews:r'")
        table_index.initialize(self.store)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT name FROM table_names WHERE goods_id='1'").fetchone()[0], "旧")
        old, new = self.correlated_names()
        self.assertEqual(old, new)
        self.store.write_cache(self.sid, "orders:k", {"rows": [{"id": "o1", "goods_id": "1"}]})
        with self.store.connect() as db:
            db.execute("DELETE FROM business_cache WHERE cache_key='products:p'")
        table_index.initialize(self.store)
        old, new = self.correlated_names()
        self.assertEqual(old, new)

    def test_update_keeps_operators_without_listing_shops(self):
        with patch.object(self.store, "list", side_effect=AssertionError("listed all shops")):
            self.store.update(self.sid, "fixture", browser="system", name="店铺乙")
        self.assertEqual(sorted(self.store.list()[0]["operators"]), sorted(["运营甲", "运营乙"]))

    def test_attention_row_count_sidecar_tracks_all_writers(self):
        def count():
            with self.store.connect() as db:
                return db.execute(
                    "SELECT row_count FROM attention_snapshot_stats WHERE shop_id=? AND kind=?", (self.sid, "tickets")
                ).fetchone()

        rows = [{"id": str(i), "status": "待处理", "attention_status": "2小时内将逾期"} for i in range(3)]
        attention.publish(
            self.store, self.sid, "tickets", {"rows": rows, "complete": True, "_sync": {"snapshot_started_at": 10}}
        )
        self.assertEqual(count()[0], 3)
        attention.reconcile(
            self.store,
            self.sid,
            "tickets",
            {"rows": [{"id": "0", "status": "已完结"}], "complete": True, "_sync": {"snapshot_started_at": 20}},
        )
        self.assertEqual(count()[0], 2)
        with self.store.connect() as db:
            # Older releases insert positionally; the sidecar keeps the snapshot table shape unchanged.
            db.execute(
                "INSERT OR REPLACE INTO attention_snapshots VALUES(?,?,?,?,?)",
                (self.sid, "tickets", json.dumps({"rows": [{}]}), 1, 1),
            )
        self.assertEqual(count()[0], 1)
        with self.store.connect() as db:
            db.execute("DELETE FROM attention_snapshot_stats")
        attention.initialize(self.store)
        with self.store.connect() as db:
            expected = db.execute(
                "SELECT json_array_length(payload,'$.rows') FROM attention_snapshots WHERE shop_id=? AND kind='tickets'",
                (self.sid,),
            ).fetchone()[0]
        self.assertEqual(count()[0], expected)
        self.store.delete(self.sid)
        self.assertIsNone(count())


if __name__ == "__main__":
    unittest.main()
