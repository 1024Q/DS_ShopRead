import asyncio, tempfile, time, unittest
from pathlib import Path
from backend.center.source_delivery import describe
from backend.center.replication_worker import Delivery
from backend.center.delivery_notify import listen, notify
from backend.source_pages import source_page


class SourceDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_notification_wakes_upload_immediately(self):
        with tempfile.TemporaryDirectory() as folder:
            event = asyncio.Event()
            transport = await listen(Path(folder), [event])
            try:
                notify(folder)
                await asyncio.wait_for(event.wait(), 0.5)
            finally:
                transport.close()

    def test_receipts_distinguish_versions_and_order_view(self):
        key = Delivery.key(1, "orders:a")
        view = Delivery.key(1, "orders:view-v1:0")
        saved = {"business": {"acks": {key: 10, view: 9}, "confirmed": {key: 20, view: 19}}}
        args = (
            saved,
            1,
            "orders:a",
            {"updated": 10},
            {"_order_lane": "recent"},
            {},
            {"cache_key": "orders:view-v1:0", "updated": 10},
        )
        self.assertEqual(describe(*args)["state"], "pending")
        saved["business"]["acks"][view] = 10
        self.assertEqual(describe(*args)["state"], "confirmed")
        self.assertEqual(describe(*args)["confirmed_at"], 20)
        self.assertEqual(describe(saved, 1, "orders:a", {"updated": 11}, {}, {})["state"], "pending")
        saved["business"]["retries"] = {key: {"retry_at": 30, "attempts": 2, "error": "offline"}}
        self.assertEqual(describe(saved, 1, "orders:a", {"updated": 11}, {}, {})["retry_at"], 30)
        saved["business"]["inflight"] = {key: {"updated": 11}}
        self.assertEqual(describe(saved, 1, "orders:a", {"updated": 11}, {}, {})["state"], "uploading")

    def test_checkpoints_are_not_center_confirmed(self):
        self.assertEqual(describe({}, 1, "orders:a", {}, {}, {"loaded": 100})["state"], "checkpoint")

    def test_order_metadata_ack_cannot_confirm_unuploaded_rows(self):
        view = {"cache_key": "orders:view-v2:0", "updated": 10, "order_stream": "stream", "order_revision": 100}
        saved = {
            "business": {"acks": {Delivery.key(1, view["cache_key"]): 10}},
            "orders": {"stream": "stream", "confirmed_revision": 99},
        }
        args = (saved, 1, "orders:a", {}, {"_order_lane": "recent"}, {"loaded": 50}, view)
        self.assertEqual(describe(*args)["state"], "pending")
        saved["orders"]["confirmed_revision"] = 100
        self.assertEqual(describe(*args)["state"], "confirmed")

    def test_complete_compact_source_snapshot_is_not_paginated(self):
        rows = [
            {"shop_id": n, "kind": "orders", "updated_at": "today", "failed_scopes": [{"error": "x"}]}
            for n in range(100)
        ]
        result = source_page({"sources": rows}, snapshot=True)
        self.assertTrue(result["snapshot_complete"])
        self.assertEqual(len(result["rows"]), 100)
        self.assertNotIn("failed_scopes", result["rows"][0])
        self.assertEqual(result["rows"][0]["failed_scope_count"], 1)
