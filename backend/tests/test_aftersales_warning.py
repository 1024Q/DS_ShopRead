import tempfile
import unittest
from backend.operations import normalize_operation
from backend.business import filter_rows
from backend import table_index
from backend.store import Store


class WarningStateTests(unittest.TestCase):
    def raw(self, title):
        return {"id": 1, "orderSn": "fixture", "afterSalesTitle": title}

    def test_warning_keeps_actual_merchant_status(self):
        row = normalize_operation("aftersales", self.raw("补寄，待商家处理"), "待处理即将逾期")[0]
        self.assertEqual(row["status"], "补寄，待商家处理")
        self.assertEqual(row["attention_status"], "待处理即将逾期")

    def test_finished_buyer_platform_and_unknown_are_not_warning(self):
        for title in (
            "退款，退款成功",
            "退款，退款关闭",
            "补寄，确认完成补寄",
            "平台处理中",
            "退货退款，待买家退货",
            "未知",
            "",
        ):
            with self.subTest(title=title), self.assertRaisesRegex(ValueError, "筛选状态不一致"):
                normalize_operation("aftersales", self.raw(title), "待处理即将逾期")

    def test_old_cached_labels_filtered_in_both_read_paths(self):
        rows = [
            {"id": str(i), "status": "待处理即将逾期", "progress": title}
            for i, title in enumerate(("退款，退款成功", "平台处理中", "补寄，待商家处理", "未知"))
        ]
        self.assertEqual([r["id"] for r in filter_rows(rows, "aftersales", status="待处理即将逾期")], ["2"])
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("fixture", "pw")
            shop = store.list()[0]
            store.write_cache(sid, "aftersales:warning", {"rows": rows, "complete": True})
            result = table_index.query(store, [(shop, "aftersales:warning")], "aftersales", status="待处理即将逾期")
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["rows"][0]["id"], "2")
            self.assertEqual(result["rows"][0]["status"], "补寄，待商家处理")
            self.assertEqual(result["rows"][0]["attention_status"], "待处理即将逾期")

    def test_ordinary_unknown_status_remains_visible(self):
        row = normalize_operation("aftersales", self.raw("未知状态"))[0]
        self.assertEqual(row["status"], "未知状态")
