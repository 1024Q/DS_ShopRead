import copy
import json
import tempfile
import unittest
from pathlib import Path
from backend.transactions import fields, normalize, number, options_for, summarize
from backend.trade_font import recognize
from backend.business import BusinessService
from backend.store import Store
from backend.browser import Session


def source():
    return {
        "getMallTradeInfo": {
            "payOrdrAmt": "1,234.56",
            "payOrdrCnt": "10",
            "payOrdrUsrCnt": "8",
            "payUvRto": "25%",
            "payOrdrAup": "154.32",
            "sucRfOrdrAmt1d": "0",
        },
        "queryMallTradeList": {
            "todayRtList": [
                {"hr": "00", "payOrdrAmt": 12.34, "payUvRto": 0.25},
                {"hr": "01", "payOrdrAmt": 34.56, "payUvRto": 0.3},
            ],
            "todayPerHourRtList": [{"hr": "01", "payOrdrAmt": 22.22}],
        },
        "queryMallConfigurationList": {
            "currentDataVOList": [{"year": 2026, "month": 1, "payOrdrAmt": "12.34"}],
            "historyDataVOList": [{"year": 2025, "month": 2, "payOrdrAmt": "5"}],
        },
        "getMallNotPayOrderInfoV2": {
            "notPayOrderCnt": 0,
            "notPayOrderAmountCnt": 101,
            "settlementShippingAmount": 12345,
        },
        "getLeadPayOrderInfo": {"allLeadPayPplCnt1m": 0, "allLeadPayOrdrAmt1m": 1234},
        "queryMallGeographyDistributionList": {
            "geographyDistributionVOList": [
                {
                    "provinceId": 1,
                    "provinceName": "测试省",
                    "statDate": "2026-10-07",
                    "cfmOrdrAmt": 622.43,
                    "cfmOrdrAmtCent": 62242,
                }
            ]
        },
    }


class TransactionTests(unittest.TestCase):
    def test_dashboard_keeps_payment_summary_and_filters_platform(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.business import create_router

        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.add("pdd-payment", "pw")
            store.add("taobao-payment", "pw", platform="taobao")
            sessions = {s["id"]: Session(s, store) for s in store.list()}
            service = BusinessService(store, sessions.get)
            options = options_for("realtime")
            for sid, session in sessions.items():
                session.state = "logged_in"
                payload = normalize(source(), {}, options, options["day"] + " 12:00:00")
                store.write_cache(sid, service.key("transactions", options, session.shop), payload)
            app = FastAPI()
            app.include_router(create_router(service, lambda: None))
            with TestClient(app) as client:
                result = client.get("/api/data/transactions?dashboard=true&trade_range=realtime&platform=pdd")
                self.assertEqual(result.status_code, 200)
                data = result.json()
                self.assertEqual(len(data["metrics"]), 1)
                self.assertEqual(data["metrics"][0]["summary"]["sales"], 1234.56)
                self.assertEqual(data["metrics"][0]["summary"]["orders"], 10)
                self.assertEqual(data["metrics"][0]["summary"]["buyers"], 8)
                self.assertEqual(data["scope"]["day"], options["day"])
                self.assertEqual(data["rows"], [])
                self.assertNotIn("annual", data["metrics"][0])

    def test_font_templates_recognized_and_unknown_rejected(self):
        templates = json.loads((Path(__file__).parents[1] / "trade_digits.json").read_text())
        masks = {chr(0xE000 + int(d)): mask for d, mask in templates.items()}
        mapping = recognize(masks)
        self.assertEqual("".join(masks).translate(mapping), "".join(templates))
        with self.assertRaises(ValueError):
            recognize({"\ue000": "0"})
        with self.assertRaises(ValueError):
            recognize({"\ue000": templates["1"], "\ue001": templates["1"]})

    def test_number_does_not_turn_missing_into_zero_or_guess_unknown_font(self):
        self.assertIsNone(number("--"))
        self.assertEqual(number("0.00"), 0)
        for value in ("\ue000", "NaN", "Infinity", True):
            with self.assertRaises(ValueError):
                number(value)
        self.assertEqual(number("12,345.67"), 12345.67)

    def test_units_rates_and_annual_data_without_goals(self):
        result = normalize(source(), {}, {"trade_range": "realtime", "day": "2026-10-08"}, "2026-10-08 12:00:00")
        metrics = result["metrics"]
        self.assertEqual(metrics["summary"]["conversion"], 25)
        self.assertEqual(result["rows"][0]["conversion"], 25)
        self.assertEqual(metrics["orders_info"]["pending_amount"], 1.01)
        self.assertEqual(metrics["orders_info"]["settlement_amount"], 123.45)
        self.assertEqual(metrics["lead"]["sales"], 12.34)
        self.assertEqual(metrics["regions"][0]["sales"], 622.43)
        self.assertEqual(len(metrics["annual"]), 2)
        self.assertEqual(metrics["annual"][0]["sales"], 12.34)
        self.assertIsNone(metrics["annual"][0]["target"])
        self.assertIsNone(metrics["annual"][1]["sales"])

    def test_hourly_and_cumulative_are_not_summed_into_summary(self):
        result = normalize(source(), {}, {"trade_range": "realtime", "day": "2026-10-08"}, "now")
        self.assertEqual(result["metrics"]["summary"]["sales"], 1234.56)
        self.assertEqual([r["series"] for r in result["rows"]], ["今日累计", "今日累计", "今日分时"])
        self.assertEqual(summarize([result["metrics"], result["metrics"]])["sales"], 2469.12)
        self.assertIsNone(summarize([])["sales"])

    def test_invalid_ranges_and_incomplete_source(self):
        with self.assertRaises(ValueError):
            options_for("invalid")
        self.assertIn("day", options_for("realtime"))
        raw = source()
        del raw["getMallTradeInfo"]["payOrdrAmt"]
        with self.assertRaises(ValueError):
            normalize(raw, {}, options_for("realtime"), None)

    def test_database_reopens_with_transaction_details(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            sid = store.add("fixture", "pw")
            record = store.list()[0]
            sessions = {sid: Session(record, store)}
            service = BusinessService(store, sessions.get)
            opts = options_for("week")
            payload = normalize(source(), {}, opts, "source time")
            key = service.key("transactions", opts, record)
            store.write_cache(sid, key, payload)
            reopened = Store(directory)
            service = BusinessService(reopened, sessions.get)
            data = service.data("transactions", opts)
            self.assertEqual(data["metrics"][0]["summary"]["sales"], 1234.56)
            self.assertEqual(len(data["metrics"][0]["annual"]), 2)
            self.assertTrue(data["sources"][0]["stale"])
            self.assertNotEqual(key, service.key("transactions", options_for("month"), record))
