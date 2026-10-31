import asyncio, json, tempfile, unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from backend.store import Store
from backend import invoice_import_store as ledger
from backend.invoice_pdf import conflicts, cents
from backend.invoice_upload import UploadWorker

PARSED = dict(
    invoice_number="12345678901234567890",
    invoice_date="2026-10-11",
    amount_cents=980,
    buyer_name="购买公司",
    buyer_tax="BUYER12345678901234",
    seller_name="销售公司",
    seller_tax="SELLER1234567890123",
    title_type="企业",
    invoice_kind=0,
    order_numbers=[],
    pages=1,
)
ENTITY = dict(company_name="销售公司", credit_code="SELLER1234567890123")
ROW = dict(
    id="serial1",
    order_number="220803-128513504192763",
    status="待开票",
    amount=9.8,
    invoice_title="购买公司",
    buyer_taxpayer_number="BUYER12345678901234",
    invoice_title_type="企业",
)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("test", "pw")
        self.store.write_cache(self.sid, "invoices:all", dict(rows=[ROW], complete=True))
        self.batch = ledger.create_batch(self.store)
        self.p = patch("backend.invoice_import_store.entity", return_value=ENTITY)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def add(self, content=b"%PDF-test", parsed=None):
        return ledger.add_file(self.store, self.batch, "invoice.pdf", content, PARSED if parsed is None else parsed)

    def test_import_is_review_only_and_batch_omits_content(self):
        f = self.add()
        self.assertEqual(f["state"], "ready")
        self.assertEqual(f["selected"]["shop_id"], self.sid)
        self.assertNotIn("content", ledger.get_file(self.store, f["id"]))
        self.assertNotIn("content", ledger.batch(self.store, self.batch)["files"][0])
        with self.store.connect() as db:
            self.assertEqual(db.execute("select count(*) from invoice_upload_claims").fetchone()[0], 0)

    def test_duplicate_does_not_poison_original(self):
        first = self.add()
        second = self.add()
        self.assertEqual(second["state"], "exception")
        self.assertEqual(ledger.match(self.store, first["id"])["state"], "ready")

    def test_conflicting_order_or_title_amount_blocks(self):
        for changes in [
            dict(amount_cents=1000),
            dict(title_type="个人"),
            dict(buyer_tax="BAD"),
            dict(seller_tax="OTHER"),
            dict(order_numbers=["other"]),
        ]:
            self.assertTrue(conflicts({**PARSED, **changes}, ROW, ENTITY))
        self.assertEqual(cents("9.80"), 980)
        self.assertIsNone(cents("9.801"))

    def test_ambiguous_needs_explicit_choice(self):
        self.store.write_cache(
            self.sid, "invoices:all", dict(rows=[ROW, {**ROW, "id": "serial2", "order_number": "other"}], complete=True)
        )
        f = self.add()
        self.assertEqual(f["state"], "exception")
        self.assertEqual(len(f["candidates"]), 2)
        chosen = ledger.match(self.store, f["id"], {"shop_id": self.sid, "id": "serial2"})
        self.assertEqual(chosen["state"], "ready")
        self.assertEqual(chosen["selected"]["id"], "serial2")

    def test_confirmation_is_revision_bound_and_cannot_repeat(self):
        f = self.add()
        items = [dict(id=f["id"], revision=f["revision"])]
        with self.assertRaises(ValueError):
            ledger.confirm(self.store, self.batch, [dict(id=f["id"], revision=0)])
        result = ledger.confirm(self.store, self.batch, items)
        self.assertEqual(result["files"][0]["state"], "queued")
        with self.assertRaises(ValueError):
            ledger.confirm(self.store, self.batch, items)

    def test_two_invoices_for_same_order_roll_back_whole_confirmation(self):
        a = self.add()
        b = self.add(b"%PDF-second", {**PARSED, "invoice_number": "22345678901234567890"})
        with self.assertRaises(ValueError):
            ledger.confirm(self.store, self.batch, [dict(id=f["id"], revision=f["revision"]) for f in (a, b)])
        self.assertTrue(all(f["state"] == "ready" for f in ledger.batch(self.store, self.batch)["files"]))
        with self.store.connect() as db:
            self.assertEqual(db.execute("select count(*) from invoice_upload_claims").fetchone()[0], 0)


class FakePage:
    def __init__(self, item):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def open(self, kind):
        pass


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sid = self.store.add("test", "pw")
        self.store.write_cache(self.sid, "invoices:all", dict(rows=[ROW], complete=True))
        self.entity_patch = patch("backend.invoice_import_store.entity", return_value=ENTITY)
        self.entity_patch.start()
        bid = ledger.create_batch(self.store)
        self.f = ledger.add_file(self.store, bid, "invoice.pdf", b"%PDF-test", PARSED)
        self.item = SimpleNamespace(shop=self.store.list()[0], lock=asyncio.Lock(), state="logged_in", control_until=0)
        self.worker = UploadWorker(self.store, lambda _: self.item)
        self.page = patch("backend.invoice_upload.ReadPage", FakePage)
        self.page.start()
        self.bid = bid

    async def asyncTearDown(self):
        self.page.stop()
        self.entity_patch.stop()
        self.tmp.cleanup()

    def queue(self):
        ledger.confirm(self.store, self.bid, [dict(id=self.f["id"], revision=self.f["revision"])])

    async def test_unconfirmed_file_never_calls_platform(self):
        with patch("backend.invoice_upload.application", new_callable=AsyncMock) as app:
            await self.worker.process(self.f["id"])
            app.assert_not_awaited()

    async def test_changed_platform_amount_blocks_before_file_upload(self):
        self.queue()
        with (
            patch(
                "backend.invoice_upload.application",
                new=AsyncMock(return_value=({"invoice_kind": 0}, {**ROW, "amount": 10})),
            ),
            patch("backend.invoice_upload.upload_pdf", new_callable=AsyncMock) as upload,
        ):
            await self.worker.process(self.f["id"])
            upload.assert_not_awaited()
        self.assertEqual(ledger.get_file(self.store, self.f["id"])["state"], "exception")

    async def test_success_requires_readback_invoice_number_and_updates_cache(self):
        self.queue()
        success = {**ROW, "status": "开票成功", "invoice_number": PARSED["invoice_number"]}
        with (
            patch(
                "backend.invoice_upload.application",
                new=AsyncMock(side_effect=[({"invoice_kind": 0}, ROW), ({}, success)]),
            ),
            patch("backend.invoice_upload.upload_pdf", new=AsyncMock(return_value="https://pfile.pddpic.com/a.pdf")),
            patch("backend.invoice_upload.submit", new_callable=AsyncMock) as submit,
            patch("backend.invoice_upload.asyncio.sleep", new_callable=AsyncMock),
        ):
            await self.worker.process(self.f["id"])
            submit.assert_awaited_once()
        self.assertEqual(ledger.get_file(self.store, self.f["id"])["state"], "success")
        self.assertEqual(self.store.read_cache(self.sid, "invoices:all")["payload"]["rows"][0]["status"], "开票成功")

    async def test_submit_timeout_does_not_resubmit(self):
        self.queue()
        with (
            patch("backend.invoice_upload.application", new=AsyncMock(return_value=({"invoice_kind": 0}, ROW))),
            patch("backend.invoice_upload.upload_pdf", new=AsyncMock(return_value="https://pfile.pddpic.com/a.pdf")),
            patch("backend.invoice_upload.submit", new=AsyncMock(side_effect=TimeoutError)) as submit,
        ):
            await self.worker.process(self.f["id"])
            self.assertEqual(ledger.get_file(self.store, self.f["id"])["state"], "verify_pending")
            await self.worker.process(self.f["id"], True)
            submit.assert_awaited_once()
