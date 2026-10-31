import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from backend.incremental_collection import read
from backend.collection_checkpoint import current


class ReportGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_reports_bypass_timestamp_delta(self):
        for kind in ("ad_reports", "ad_products", "transactions", "goodsdata", "overview"):
            reader = AsyncMock(return_value={"rows": [], "complete": True})
            old = {
                "payload": {
                    "rows": [],
                    "complete": True,
                    "_sync": {"snapshot_started_at": time.time(), "full_at": time.time()},
                }
            }
            result = await read(
                SimpleNamespace(shop={"platform": "pdd"}),
                kind,
                {"date_from": "2026-10-13", "date_to": "2026-10-13"},
                old,
                {"daily_time": "03:00"},
                False,
                reader,
            )
            reader.assert_awaited_once()
            self.assertFalse(result["_sync"]["incremental_supported"])

    async def test_old_checkpoint_cannot_force_unsupported_delta(self):
        checkpoint = SimpleNamespace(data={"incremental": True, "started_at": time.time()})
        token = current.set(checkpoint)
        try:
            reader = AsyncMock(return_value={"rows": [], "complete": True})
            await read(
                SimpleNamespace(shop={"platform": "pdd"}),
                "ad_reports",
                {},
                None,
                {"daily_time": "03:00"},
                False,
                reader,
            )
            self.assertFalse(checkpoint.data["incremental"])
            reader.assert_awaited_once()
        finally:
            current.reset(token)
