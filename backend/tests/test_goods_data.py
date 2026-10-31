import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from backend.goods_data import (
    normalize_row,
    normalize,
    sort_rows,
    summarize,
    options_for,
    collect,
    LIVE,
    HISTORY,
    DETAIL,
    DAY_TREND,
)
from backend.business import BusinessService
from backend.browser import Session
from backend.store import Store


def raw():
    return {
        LIVE: {
            "todayData": {"guv": "10", "gpv": "30", "payOrdrAmt": "100.25"},
            "yesData": {"guv": "20"},
            "todayList": [{"guv": 5, "statHr": "00", "payUvRto": 0.25}],
            "yesList": [],
        },
        HISTORY: {"guv": "20", "payOrdrAmt": "200", "goodsFavCnt": "3"},
        DAY_TREND: [{"statHr": "00", "guv": 2, "payUvRto": 0.1}],
        DETAIL: {
            "goodsDetailList": [
                {
                    "goodsId": 123,
                    "goodsName": "测试商品",
                    "goodsUv": "10",
                    "payOrdrAmt": "1,234.56",
                    "goodsVcr": "12.50%",
                }
            ],
            "totalNum": 1,
        },
    }


class GoodsDataTests(unittest.TestCase):
    def test_units_missing_and_large_ids(self):
        row = normalize_row(
            {
                "goodsId": 998877665544332211,
                "goodsUv": "0",
                "payOrdrAmt": "1,234.56",
                "goodsVcr": "12.50%",
                "goodsUvYtd": "5",
                "goodsUvPpr": 0.1855,
                "goodsUvPprIsPercent": True,
            },
            {},
        )
        self.assertEqual(row["goods_id"], "998877665544332211")
        self.assertEqual(row["visitors"], 0)
        self.assertEqual(row["sales"], 1234.56)
        self.assertEqual(row["conversion"], 12.5)
        self.assertIsNone(row["quantity"])
        self.assertEqual(row["previous"]["visitors"], 5)
        self.assertAlmostEqual(row["comparison"]["visitors"]["value"], 18.55)

    def test_live_favorites_are_yesterday_and_trend_rates_are_fractional(self):
        data = normalize(raw(), raw()[DETAIL]["goodsDetailList"], {}, options_for("realtime"), "today", 1, True)
        self.assertEqual(data["metrics"]["summary"]["favorites"], 3)
        self.assertEqual(data["metrics"]["summary"]["sales"], 100.25)
        self.assertEqual(data["metrics"]["trend"][0]["conversion"], 25)
        self.assertEqual(data["metrics"]["trend"][0]["hour"], "00")

    def test_yesterday_uses_its_hourly_series(self):
        data = normalize(raw(), [], {}, options_for("yesterday"), "yesterday", 0, True)
        self.assertEqual(data["metrics"]["summary"]["sales"], 200)
        self.assertEqual(data["metrics"]["trend"][0]["series"], "昨日累计")
        self.assertEqual(data["metrics"]["trend"][0]["conversion"], 10)

    def test_sort_keeps_unknown_last_in_both_directions(self):
        rows = [{"id": "a", "sales": None}, {"id": "b", "sales": 0}, {"id": "c", "sales": 100}]
        self.assertEqual([r["id"] for r in sort_rows(rows, "sales", "desc")], ["c", "b", "a"])
        self.assertEqual([r["id"] for r in sort_rows(rows, "sales", "asc")], ["b", "c", "a"])
        with self.assertRaises(ValueError):
            sort_rows(rows, "secret", "desc")
        self.assertIsNone(summarize([])["sales"])
        self.assertNotIn("conversion", summarize([{"summary": {"conversion": 50}}]))

    def test_source_summary_not_sum_of_goods_and_database_reopens(self):
        data = normalize(raw(), raw()[DETAIL]["goodsDetailList"], {}, options_for("week"), "week", 2, False)
        self.assertEqual(data["metrics"]["summary"]["sales"], 200)
        self.assertEqual(data["rows"][0]["sales"], 1234.56)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            sid = store.add("fixture", "pw")
            shop = store.list()[0]
            service = BusinessService(store, lambda _: Session(shop, store))
            opts = options_for("week")
            key = service.key("goodsdata", opts, shop)
            store.write_cache(sid, key, data)
            reread = Store(directory).read_cache(sid, key)["payload"]
            self.assertFalse(reread["complete"])
            self.assertEqual(reread["source_total"], 2)
            self.assertEqual(reread["rows"][0]["sales"], 1234.56)


class PagingTests(unittest.IsolatedAsyncioTestCase):
    async def test_collects_remaining_pages_and_deduplicates(self):
        first = raw()
        first[DETAIL]["totalNum"] = 2
        next_page = {
            DETAIL: {
                "totalNum": 2,
                "goodsDetailList": first[DETAIL]["goodsDetailList"] + [{"goodsId": 456, "goodsUv": 2}],
            }
        }

        class Page:
            trade_responses = {}
            evaluate = AsyncMock(side_effect=[False, True, "platform time"])

        with (
            patch("backend.goods_data.wait_responses", AsyncMock(side_effect=[first, next_page])),
            patch("backend.goods_data.display_map", AsyncMock(return_value={})),
        ):
            data = await collect(Page(), options_for("realtime"))
        self.assertEqual([r["id"] for r in data["rows"]], ["123", "456"])
        self.assertTrue(data["complete"])

    async def test_repeated_page_never_reported_complete(self):
        first = raw()
        first[DETAIL]["totalNum"] = 3

        class Page:
            trade_responses = {}
            evaluate = AsyncMock(side_effect=[False, True])

        with patch("backend.goods_data.wait_responses", AsyncMock(side_effect=[first, {DETAIL: first[DETAIL]}])):
            with self.assertRaisesRegex(ValueError, "翻页未前进"):
                await collect(Page(), options_for("realtime"))

    async def test_official_larger_page_avoids_cross_page_rank_duplicates(self):
        first = raw()
        first[DETAIL]["totalNum"] = 25
        expanded = {DETAIL: {"totalNum": 25, "goodsDetailList": [{"goodsId": i, "goodsUv": i} for i in range(25)]}}

        class Page:
            trade_responses = {}
            evaluate = AsyncMock(side_effect=[True, True, "platform time"])

        with (
            patch("backend.goods_data.wait_responses", AsyncMock(side_effect=[first, expanded])),
            patch("backend.goods_data.display_map", AsyncMock(return_value={})),
        ):
            data = await collect(Page(), options_for("realtime"))
        self.assertEqual(len(data["rows"]), 25)
        self.assertTrue(data["complete"])
