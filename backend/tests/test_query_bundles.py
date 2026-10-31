import unittest
from backend.query_bundles import catalog, project
from backend.frequency_policy import step_catalog


class QueryBundles(unittest.TestCase):
    def test_catalog_exposes_one_query_without_losing_range_identities(self):
        raw = step_catalog()
        result = catalog(raw)
        self.assertEqual(len(raw) - len(result), 12)
        history = next(s for s in result if s["id"] == "ad_reports@history_query")
        self.assertEqual(history["members"], ["ad_reports@yesterday", "ad_reports@week", "ad_reports@month"])
        self.assertIn("ad_reports@base", {s["id"] for s in result})
        self.assertIn("aftersales@expiring", {s["id"] for s in result})

    def test_progress_keeps_current_range_and_retry_checkpoint(self):
        steps = [{"kind": "goodsdata", "scope": {"trade_range": span}, "cache_key": span,
                  "phase": phase, "frequency": freq, "name": span, "stage": "读取中", "continuation": True}
                 for span, phase, freq in (("yesterday", "waiting", "15分钟"), ("week", "running", "1小时"), ("month", "waiting", "1小时"))]
        result = project(steps)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["cache_key"], "week")
        self.assertEqual(result[0]["range_steps"], steps)
        self.assertTrue(result[0]["continuation"])
        self.assertIn("15分钟", result[0]["frequency"])


if __name__ == "__main__":
    unittest.main()
