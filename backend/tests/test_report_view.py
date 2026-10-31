import tempfile, unittest, zipfile
from pathlib import Path
from sqlalchemy import create_engine
from backend.center.report_view import month_dates, import_report, query


class ReportViewTests(unittest.TestCase):
    def test_month_year_and_leap_boundaries(self):
        self.assertEqual(month_dates("2026-09"), ("2026-09-01 00:00:00", "2026-10-01 00:00:00"))
        self.assertEqual(month_dates("2026-12")[1], "2027-01-01 00:00:00")
        self.assertEqual(month_dates("2024-02")[1], "2024-03-01 00:00:00")
        with self.assertRaises(ValueError):
            month_dates("2026-13")

    def test_strings_pagination_sort_filters_and_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = create_engine("sqlite:///" + str(Path(folder) / "data.db"))
            path = Path(folder) / "report.zip"
            headers = (
                "商品,订单号,订单状态,商品数量(件),商家编码-规格维度,商家编码-商品维度,商家实收金额(元),快递单号\n"
            )

            def package(body):
                with zipfile.ZipFile(path, "w") as z:
                    z.writestr("店铺.csv", (headers + body).encode("utf-8-sig"))
                    z.writestr("原始文件/old.zip", b"ignored")

            a = {
                "id": "a" * 32,
                "shop_id": 1,
                "shop_name": "店铺甲",
                "created": 1,
                "start": month_dates("2026-09")[0],
                "end": month_dates("2026-09")[1],
            }
            package(
                "商品甲,001234567890123456789,待发货,2,001,002,10.01,009999999999999999999\n商品乙,123,已发货,1,A,B,2.50,003\n"
            )
            self.assertEqual(import_report(engine, a, path), 2)
            result = query(engine, [a["id"]], month="2026-09", sort_by=6, page_size=1)
            self.assertEqual(result["total"], 2)
            self.assertEqual(result["rows"][0]["cells"][6], "2.50")
            result = query(engine, [a["id"]], keyword="001234567890123456789", status="待发货")
            self.assertEqual(result["rows"][0]["cells"][1], "001234567890123456789")
            b = {**a, "id": "b" * 32, "created": 2}
            package("新商品,456,已收货,1,A,B,3.00,004\n")
            import_report(engine, b, path)
            result = query(engine, [a["id"], b["id"]])
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["rows"][0]["cells"][0], "新商品")
            c = {**a, "id": "c" * 32, "shop_id": 2, "shop_name": "店铺乙"}
            import_report(engine, c, path)
            self.assertEqual(query(engine, [a["id"], b["id"], c["id"]])["total"], 2)
            self.assertEqual(query(engine, [a["id"], b["id"], c["id"]], shop_id=1)["total"], 1)
            self.assertEqual(query(engine, [a["id"]], keyword="%")["total"], 0)
            engine.dispose()

    def test_xlsx_identifier_read_without_float_conversion(self):
        import io
        from backend.center.report_view import sheet_rows

        file = io.BytesIO()
        with zipfile.ZipFile(file, "w") as z:
            z.writestr(
                "xl/worksheets/sheet1.xml",
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1" t="inlineStr"><is><t>订单号</t></is></c><c r="B1"><v>001234567890123456789</v></c></row></sheetData></worksheet>',
            )
        self.assertEqual(list(sheet_rows(file.getvalue(), ".xlsx"))[0][1], "001234567890123456789")
