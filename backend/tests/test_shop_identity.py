import asyncio
import io
import tempfile
import unittest
from types import SimpleNamespace
from zipfile import ZipFile
from backend.store import Store
from backend.business import BusinessService, options_for
from backend import tax_info
from backend.xlsx_export import response
from backend.shop_identity import identity, shop_label
from backend.tests.test_audit_store import row


class ShopIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.pdd = self.store.add("pdd-test", "pw", name="同名店", platform="pdd")
        self.jd = self.store.add("jd-test", "pw", name="同名店", platform="jd")

    def tearDown(self):
        self.tmp.cleanup()

    def test_logs_keep_platform_after_rename_delete_and_search(self):
        for sid in (self.pdd, self.jd):
            self.store.log_event("info", "login", "ok", "测试事件", sid)
        from backend.log_spool import get as log_spool

        log_spool(self.store).flush(self.store)
        self.assertEqual({r["shop_label"] for r in self.store.read_logs()}, {"拼多多 · 同名店", "京东 · 同名店"})
        from backend.log_spool import get as log_spool

        log_spool(self.store).flush(self.store)
        self.assertEqual([r["shop_id"] for r in self.store.read_logs(keyword="京东")], [self.jd])
        self.store.update(self.jd, "jd-test", name="改名店", platform="taobao")
        self.store.log_event("info", "login", "ok", "测试事件", self.jd)
        self.store.delete(self.jd)
        from backend.log_spool import get as log_spool

        log_spool(self.store).flush(self.store)
        logs = Store(self.tmp.name).read_logs(shop_id=self.jd)
        self.assertEqual({r["shop_label"] for r in logs}, {"京东 · 同名店", "淘宝 · 改名店"})
        self.store.log_event("info", "system", "ok", "系统事件")
        from backend.log_spool import get as log_spool

        log_spool(self.store).flush(self.store)
        self.assertEqual(self.store.read_logs()[0]["shop_label"], "系统")

    def test_legacy_database_logs_are_preserved_but_not_used_for_new_file_feed(self):
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO event_logs(created,last_seen,level,module,event,shop_id,shop_name,message) VALUES(1,1,'info','login','old',?,'旧店','旧记录')",
                (self.pdd,),
            )
        reopened = Store(self.tmp.name)
        self.assertEqual(reopened.read_logs(), [])
        with reopened.connect() as db:
            old = dict(db.execute("SELECT * FROM event_logs").fetchone())
            self.assertEqual(old["message"], "旧记录")
            self.assertEqual(old["platform"], "")

    def test_business_and_export_keep_original_name_and_add_label(self):
        service = BusinessService(self.store, lambda sid: SimpleNamespace(state="stopped", retired=False))
        record = next(r for r in self.store.list() if r["id"] == self.pdd)
        key = service.key("products", options_for("products"), record)
        self.store.write_cache(self.pdd, key, {"rows": [{"id": "123", "name": "商品"}], "complete": True})
        data = service.data("products", options_for("products"))
        self.assertEqual(data["rows"][0]["shop_name"], "同名店")
        self.assertEqual(data["sources"][0]["shop_label"], "拼多多 · 同名店")
        with ZipFile(io.BytesIO(response("products", data, {}).body)) as z:
            self.assertIn("拼多多 · 同名店", z.read("xl/worksheets/sheet1.xml").decode())

    def test_tax_same_subject_keeps_each_platform_label(self):
        entries = [
            {**row(), "shop_id": sid, "shop_name": "同名店", "platform": platform}
            for sid, platform in [(self.pdd, "pdd"), (self.jd, "jd")]
        ]
        grouped = tax_info.group_rows(entries)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["shop_label"], "拼多多 · 同名店、京东 · 同名店")
        sources = [{**r, "entity": entries[0]["entity"]} for r in entries]
        self.assertEqual(tax_info.subject_options(sources)[0]["shop_labels"], ["拼多多 · 同名店", "京东 · 同名店"])
