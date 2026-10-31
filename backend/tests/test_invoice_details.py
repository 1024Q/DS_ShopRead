import tempfile, unittest
from backend.operations import normalize_operation
from backend.store import Store
from backend import table_index


class InvoiceDetails(unittest.TestCase):
    def test_title_type_uses_platform_enum_without_guessing(self):
        from backend.invoice_fields import details

        for code, expected in [
            (0, "个人"),
            (1, "企业"),
            ("0", "个人"),
            ("1", "企业"),
            (None, ""),
            (2, ""),
            (False, ""),
        ]:
            self.assertEqual(details({"business_type": code})["invoice_title_type"], expected)
        self.assertEqual(details({"payer_register_no": "123", "letterhead": "公司"})["invoice_title_type"], "")

    def test_pending_query_uses_complete_refresh_but_all_keeps_own_scope(self):
        from backend.business import BusinessService, options_for

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("test", "pw")
            shop = store.list()[0]
            service = BusinessService(store, lambda _: None)
            options = options_for("invoices", status="待开票")
            pending = {**options, "_invoice_details_refresh": True}
            store.write_cache(sid, service.key("invoices", pending, shop), {"rows": [], "complete": True})
            self.assertEqual(service.query_options("invoices", options, shop), pending)
            self.assertFalse(
                service.query_options("invoices", options_for("invoices"), shop).get("_invoice_details_refresh")
            )

    def test_pending_backfill_preserves_status_scope_and_timestamp(self):
        from backend.invoice_fields import backfill

        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("test", "pw")
            old = {"id": "s1", "order_number": "o1", "status": "待开票", "amount": 9.8}
            store.write_cache(
                sid, "invoices:all", {"rows": [old, {"id": "other", "status": "开票成功"}], "complete": True}
            )
            before = store.read_cache(sid, "invoices:all")
            fresh = normalize_operation(
                "invoices",
                {"order_sn": "o1", "serial_no": "s1", "payer_register_no": "TAX001", "other_info": "账号：00123"},
                "待开票",
            )[0]
            backfill(store, sid, {"rows": [fresh], "complete": True})
            after = store.read_cache(sid, "invoices:all")
            self.assertEqual(before["updated"], after["updated"])
            self.assertEqual(after["payload"]["rows"][0]["buyer_bank_account"], "00123")
            self.assertEqual(after["payload"]["rows"][0]["amount"], 9.8)
            self.assertEqual(len(after["payload"]["rows"]), 2)
            self.assertEqual(
                table_index.query(store, [(store.list()[0], "invoices:all")], "invoices", keyword="TAX001")["total"], 1
            )

    def test_export_has_stable_headers_and_literal_identifiers(self):
        import io, zipfile, xml.etree.ElementTree as ET
        from backend.xlsx_export import table, workbook
        from backend.invoice_fields import EXPORT_FIELDS
        from backend.xlsx_export import LABELS, OVERRIDES

        rows = table(
            [
                {
                    "order_number": "220803-128513504192763",
                    "buyer_bank_account": "001234567890",
                    "buyer_phone": "0371-123",
                    "invoice_other_info": "=not-a-formula",
                }
            ],
            "invoices",
        )
        labels = {**LABELS, **OVERRIDES["invoices"]}
        self.assertEqual(rows[0][: len(EXPORT_FIELDS)], [labels[k] for k in EXPORT_FIELDS])
        self.assertNotIn("invoice_details_version", rows[0])
        with zipfile.ZipFile(io.BytesIO(workbook([("订单开票", rows)]))) as z:
            root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
            ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            values = [e.text for e in root.findall(".//m:t", ns)]
            self.assertIn("001234567890", values)
            self.assertIn("0371-123", values)
            self.assertFalse(root.findall(".//m:f", ns))

    def test_details_preserve_original_and_text_identifiers(self):
        info = "开户银行：测试银行;\n账号：001234567890;\n地址：测试路1号；电话：0371-00000000;\n补充说明：交付后开票"
        row = normalize_operation(
            "invoices",
            {
                "order_sn": "o1",
                "serial_no": "s1",
                "invoice_amount": 980,
                "payer_register_no": "TAX001",
                "spec": "3瓶+喷头",
                "other_info": info,
                "invoice_time": 0,
                "goods_number": None,
            },
            "待开票",
        )[0]
        self.assertEqual(row["buyer_taxpayer_number"], "TAX001")
        self.assertEqual(row["buyer_bank_account"], "001234567890")
        self.assertEqual(row["buyer_phone"], "0371-00000000")
        self.assertEqual(row["invoice_other_info"], info)
        self.assertEqual(row["amount"], 9.8)
        self.assertEqual(row["invoiced_at"], "")
        self.assertIsNone(row["quantity"])

    def test_ambiguous_bank_account_is_not_chosen(self):
        row = normalize_operation("invoices", {"order_sn": "o1", "other_info": "账号：001;银行账号：002;电话:0123"})[0]
        self.assertEqual(row["buyer_bank_account"], "")
        self.assertIn("002", row["invoice_other_info"])

    def test_new_fields_survive_database_projection_and_search(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add("invoice-test", "pw")
            row = normalize_operation(
                "invoices",
                {
                    "order_sn": "o1",
                    "payer_register_no": "TAX001",
                    "other_info": "开户银行：测试银行;账号：00123",
                    "spec": "3瓶装",
                },
            )[0]
            store.write_cache(sid, "invoices:test", {"rows": [row], "complete": True})
            result = table_index.query(store, [(store.list()[0], "invoices:test")], "invoices", keyword="TAX001")
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["rows"][0]["buyer_bank_account"], "00123")
