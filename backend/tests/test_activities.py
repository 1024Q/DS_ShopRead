import asyncio
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile
from xml.etree import ElementTree as ET
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.activities import normalize, collect
from backend.business import BusinessService, create_router, filter_rows, options_for
from backend.read_errors import PageReadError
from backend.xlsx_export import response


def raw(identifier=998877665544332211, **changes):
    return {
        "activity_goods_id": identifier,
        "goods_id": 999888777666555444,
        "activity_id": 22660,
        "goods_name": "测试商品",
        "hd_thumb_url": "https://img.pddpic.com/test.jpg",
        "activity_name": "测试活动",
        "business_type_str": "限时秒杀",
        "status": 104,
        "final_status_name": "报名被驳回",
        "enroll_time": 1791341343573,
        "refuse_reason": "测试驳回原因",
        "activity_goods_time_list": [
            {
                "activity_goods_start_time": 1791341343000,
                "activity_goods_end_time": 1791600543000,
                "min_activity_sku_price": 880,
                "max_activity_sku_price": 2150,
                "activity_quantity": 3000,
            }
        ],
        "activity_sku_info": [{"sku_id": 998877665544332212, "sku_name": "大规格", "activity_price": 2150}],
        **changes,
    }


def packet(page, items, total):
    return {"page": page, "data": {"success": True, "result": {"total": total, "result": items}}}


class ActivityTests(unittest.TestCase):
    def test_identity_price_time_and_final_platform_status(self):
        r = normalize(raw())
        self.assertEqual(r["id"], "998877665544332211")
        self.assertEqual(r["goods_id"], "999888777666555444")
        self.assertEqual(r["skus"][0]["sku_id"], "998877665544332212")
        self.assertEqual((r["price_min"], r["price_max"]), (8.8, 21.5))
        self.assertEqual(r["created_at"], "2026-10-07 10:49:03")
        self.assertEqual(r["status"], "报名被驳回")
        self.assertEqual(r["thumbnail_url"], "https://img.pddpic.com/test.jpg")
        self.assertEqual(normalize(raw(status=601, final_status_name="已放弃活动"))["status"], "已放弃活动")

    def test_missing_is_not_zero_and_unknown_status_is_not_success(self):
        r = normalize(raw(activity_goods_time_list=[], activity_sku_info=[], final_status_name="", status=999))
        self.assertIsNone(r["price_min"])
        self.assertIsNone(r["price_max"])
        self.assertIn("未知状态", r["status"])
        r = normalize(raw(activity_goods_time_list=[], activity_sku_info=[{"sku_id": 1, "activity_price": 0}]))
        self.assertEqual(r["price_min"], 0)
        with self.assertRaises(ValueError):
            normalize(raw(activity_goods_id=None))
        with self.assertRaises(ValueError):
            normalize(raw(activity_goods_time_list=[{"min_activity_sku_price": "880"}]))

    def test_filters_match_activity_and_goods_without_merging_enrollments(self):
        rows = [
            normalize(raw(1)),
            normalize(raw(2)),
            normalize(raw(3, business_type_str="百亿补贴", final_status_name="报名成功")),
        ]
        self.assertEqual(
            len(
                filter_rows(
                    rows,
                    "activities",
                    keyword="22660",
                    status="报名被驳回",
                    activity_type="限时秒杀",
                    date_from="2026-10-07",
                    date_to="2026-10-07",
                )
            ),
            2,
        )
        self.assertEqual(len(filter_rows(rows, "activities", keyword="测试活动", activity_type="百亿补贴")), 1)
        self.assertEqual(filter_rows(rows, "activities", date_from="2026-10-08"), [])
        service = BusinessService(None, None)
        shop = {"profile_version": 0}
        self.assertEqual(
            service.key("activities", options_for("activities"), shop),
            service.key("activities", options_for("activities", status="报名被驳回"), shop),
        )
        self.assertNotEqual(service.key("activities", {}, shop), service.key("activities", {}, {"profile_version": 1}))

    def test_api_filter_and_export_include_all_rows_and_nested_details(self):
        rows = [{**normalize(raw(i)), "shop_id": 1, "shop_name": "测试店"} for i in range(1, 24)]
        rows.append({**normalize(raw(24, business_type_str="百亿补贴")), "shop_id": 1, "shop_name": "测试店"})
        service = SimpleNamespace(data=lambda *a, **kw: {"rows": copy.deepcopy(rows), "sources": [], "metrics": []})
        app = FastAPI()
        app.include_router(create_router(service, lambda: None))
        with TestClient(app) as client:
            d = client.get("/api/data/activities?activity_type=限时秒杀&page_size=20").json()
            self.assertEqual(d["total"], 23)
            self.assertEqual(len(d["rows"]), 20)
            self.assertEqual(set(d["activity_types"]), {"限时秒杀", "百亿补贴"})
            r = client.get("/api/data/activities?activity_type=限时秒杀&export=true&page=2")
            self.assertEqual(r.status_code, 200)
            with ZipFile(io.BytesIO(r.content)) as z:
                ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                for name in ("sheet1", "sheet4", "sheet5"):
                    xml = ET.fromstring(z.read("xl/worksheets/" + name + ".xml"))
                    self.assertEqual(len(xml.findall("s:sheetData/s:row", ns)), 24)
                self.assertIn("SKU ID", z.read("xl/worksheets/sheet5.xml").decode())


class ActivityReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_pagination_skips_duplicate_responses_and_deduplicates_ids(self):
        queue = asyncio.Queue()
        for value in (packet(1, [raw(1), raw(2)], 3), packet(1, [raw(1), raw(2)], 3), packet(2, [raw(2), raw(3)], 3)):
            queue.put_nowait(value)
        page = SimpleNamespace(
            activities=queue,
            evaluate=AsyncMock(
                side_effect=lambda expression: (
                    json.dumps({"success": True, "result": []}) if "statusType" in expression else True
                )
            ),
        )
        with patch("backend.activities.asyncio.sleep", new=AsyncMock()):
            d = await collect(page, {})
        self.assertEqual([r["id"] for r in d["rows"]], ["1", "2", "3"])
        self.assertTrue(d["complete"])
        self.assertEqual(page.evaluate.await_count, 2)
        self.assertIn("pagination-next", page.evaluate.await_args.args[0])
        self.assertNotIn("fetch(", page.evaluate.await_args.args[0])

    async def test_native_error_retained_and_short_page_marked_partial(self):
        queue = asyncio.Queue()
        queue.put_nowait(packet(1, [raw(1)], 5))
        page = SimpleNamespace(
            activities=queue,
            evaluate=AsyncMock(
                side_effect=lambda expression: (
                    json.dumps({"success": True, "result": []}) if "statusType" in expression else False
                )
            ),
        )
        with patch("backend.activities.asyncio.sleep", new=AsyncMock()):
            d = await collect(page, {})
        self.assertFalse(d["complete"])
        self.assertEqual(d["source_total"], 5)
        queue.put_nowait({"page": 1, "data": {"success": False, "error_msg": "当前请求频繁，请2小时后重试"}})
        with self.assertRaisesRegex(ValueError, "2小时"):
            await collect(page, {})
        queue.put_nowait({"page": 1, "data": {"success": False, "error_msg": "登录已过期"}})
        with self.assertRaises(PageReadError):
            await collect(page, {})
        queue.put_nowait({"page": 1, "data": {"success": True, "result": {"total": 5}}})
        with self.assertRaisesRegex(ValueError, "格式"):
            await collect(page, {})

    async def test_empty_success_is_complete(self):
        queue = asyncio.Queue()
        queue.put_nowait(packet(1, [], 0))
        page = SimpleNamespace(
            activities=queue, evaluate=AsyncMock(return_value=json.dumps({"success": True, "result": []}))
        )
        d = await collect(page, {})
        self.assertEqual(d["rows"], [])
        self.assertTrue(d["complete"])
        page.evaluate.assert_awaited_once()
