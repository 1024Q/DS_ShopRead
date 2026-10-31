import tempfile
import unittest
import sqlite3
from pathlib import Path
from backend.store import Store
from backend.browser import Session


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_encrypted_and_persistent(self):
        shop_id = self.store.add("merchant", "secret-password", "bundled")
        self.assertNotIn("password", self.store.list()[0])
        self.assertNotIn(b"secret-password", self.store.path.read_bytes())
        self.assertEqual(Store(self.temp.name).credentials(shop_id)["password"], "secret-password")

    def test_import_once(self):
        path = Path(self.temp.name) / "legacy.txt"
        path.write_text("[店铺 1]\n账号=one\n密码=pw\n[店铺 2]\n账号=\n密码=\n", encoding="utf-8")
        self.store.import_legacy(path)
        path.write_text("[店铺 3]\n账号=two\n密码=pw\n", encoding="utf-8")
        self.store.import_legacy(path)
        self.assertEqual(len(self.store.list()), 1)

    def test_duplicate_and_validation(self):
        self.store.add("merchant", "password")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add("MERCHANT", "password")
        with self.assertRaises(ValueError):
            self.store.add("bad\nname", "password")
        with self.assertRaises(ValueError):
            self.store.add("name", "password", "other")

    def test_isolation(self):
        self.store.add("first", "pw")
        self.store.add("second", "pw")
        a, b = [Session(shop, self.store) for shop in self.store.list()]
        self.assertNotEqual(a.port, b.port)
        self.assertNotEqual(a.profile, b.profile)
