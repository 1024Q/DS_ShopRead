import json, tempfile, unittest
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend import table_index
from backend.store import Store
from backend.business import BusinessService, create_router, options_for, filter_rows
from backend.activities import groups
from backend.promotion_logs import normalize
from backend.promotion_status import label
from backend.remote_nodes import PATH


class CommerceUpdates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("fixture", "password")
        self.shop = self.store.list()[0]
        session = SimpleNamespace(shop=self.shop, state="logged_in")
        self.service = BusinessService(self.store, lambda sid: session)
        app = FastAPI()
        app.include_router(create_router(self.service, lambda: None))
        self.client = TestClient(app)

    def save(self, kind, rows, options=None):
        key = self.service.key(kind, options or options_for(kind), self.shop)
        self.store.write_cache(self.sid, key, {"rows": rows, "complete": True, "source_total": len(rows)})
        return key

    def test_brand_reason_filters_all_pages_and_export(self):
        rows = [
            {"id": str(i), "status": "无黑标商品", "problem": "价格较高；资质缺失" if i % 2 else "价格较高（其他）"}
            for i in range(45)
        ]
        self.save("brands", rows)
        result = self.client.get("/api/data/brands", params={"brand_reason": "价格较高", "page_size": 20}).json()
        self.assertEqual(result["total"], 22)
        self.assertEqual(len(result["rows"]), 20)
        self.assertEqual(set(result["brand_reasons"]), {"价格较高", "价格较高（其他）", "资质缺失"})
        self.assertEqual(len(filter_rows(rows, "brands", brand_reason="价格较高")), 22)
        export = self.client.get("/api/data/brands", params={"brand_reason": "价格较高", "export": True})
        self.assertEqual(export.status_code, 200)

    def test_activity_groups_use_platform_codes_and_pending_can_overlap(self):
        rows = [
            {"id": "a", "status_code": 501, "status": "报名成功", "wait_handle_price": True},
            {"id": "b", "status_code": 104, "status": "报名被驳回"},
            {"id": "c", "status_code": 103, "status": "审核通过"},
            {"id": "d", "status_code": 401, "sub_status_code": 101, "status": "寄样及寄样审核"},
            {"id": "e", "status_code": 401, "sub_status_code": 102, "status": "寄样及寄样审核"},
        ]
        self.save("activities", rows)
        for group, expected in [("待处理", {"a", "d"}), ("审核中", {"c"}), ("活动中", {"a"}), ("已结束", {"b"})]:
            data = self.client.get("/api/data/activities", params={"activity_group": group}).json()
            self.assertEqual({r["id"] for r in data["rows"]}, expected)
            self.assertEqual({r["id"] for r in filter_rows(rows, "activities", activity_group=group)}, expected)
        self.assertEqual(self.client.get("/api/data/activities?activity_group=invalid").status_code, 400)

    def test_old_status_projection_repaired_without_changing_taobao(self):
        from backend.promotions import options_for as ad_options

        key = self.save("ad_products", [{"id": "a", "status_code": 4, "status": "平台状态 4"}], ad_options())
        with self.store.connect() as db:
            db.execute(
                "UPDATE table_rows SET status='平台状态 4',payload=json_set(payload,'$.status','平台状态 4') WHERE cache_key=?",
                (key,),
            )
            db.execute(
                "UPDATE table_snapshots SET metadata=json_remove(metadata,'$._commerce_filters_version') WHERE cache_key=?",
                (key,),
            )
        table_index.initialize(self.store)
        data = self.client.get("/api/data/ad_products?status=商品下架").json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["rows"][0]["status"], "商品下架")
        self.assertEqual(label("2"), "手动暂停")
        self.assertEqual(label(999), "未知推广状态")

    def test_log_time_identity_search_sort_export_and_remote_route(self):
        row = normalize(
            {
                "operateTime": "2026-10-10 09:40:09",
                "operatorName": "运营甲",
                "operationObject": "推广一",
                "operationType": 2,
                "moduleType": 15,
                "content": "预算调整",
            }
        )
        self.assertEqual(row["created_at"], "2026-10-10 09:40:09")
        self.assertEqual(row["status"], "更新")
        self.assertNotEqual(
            row["id"],
            normalize(
                {
                    "operateTime": "2026-10-10 09:40:09",
                    "operatorName": "运营甲",
                    "operationObject": "推广一",
                    "operationType": 2,
                    "moduleType": 15,
                    "content": "预算调整",
                },
                4,
            )["id"],
        )
        self.save("ad_logs", [row])
        data = self.client.get("/api/data/ad_logs?keyword=运营甲&sort_by=created_at").json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(self.client.get("/api/data/ad_logs?export=true").status_code, 200)
        self.assertTrue(PATH.fullmatch("data/ad_logs/sync"))
        self.assertTrue(PATH.fullmatch("shops/1/ad_logs"))
        with self.assertRaises(ValueError):
            normalize({"operateTime": "today"})
