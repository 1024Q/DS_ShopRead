import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from backend import collection_checkpoint as cp
from backend.operations import paginate
from backend.store import Store


class InvoiceEmptyCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def read(self, response):
        with cp.context(self.store, 1, "invoices:test", {"batch_pages": 12}):
            with patch("backend.operations.read", AsyncMock(return_value=response)):
                return await paginate(None, "invoices", {"page_no": 1}, "page_no", "list")

    async def test_explicit_null_zero_is_complete_empty_category(self):
        result = await self.read({"list": None, "total": 0})
        self.assertTrue(result["complete"])
        self.assertEqual(result["source_total"], 0)
        self.assertEqual(result["rows"], [])

    async def test_null_cannot_erase_nonempty_or_unknown_totals(self):
        for response in (
            {"list": None, "total": 2},
            {"list": None},
            {"total": 0},
            {"list": {}, "total": 0},
            {"list": None, "total": False},
        ):
            with self.subTest(response=response):
                with self.assertRaisesRegex(ValueError, "列表字段"):
                    await self.read(response)
