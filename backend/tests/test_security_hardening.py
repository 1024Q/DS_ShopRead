import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import credentials_key, safe_xml
from backend.center import overview_snapshot


class SafeXmlTests(unittest.TestCase):
    def test_plain_documents_parse(self):
        self.assertEqual(safe_xml.fromstring(b"<a><b>x</b></a>").find("b").text, "x")
        self.assertEqual([e.tag for _, e in safe_xml.iterparse(io.BytesIO(b"<a><b/></a>"))], ["b", "a"])

    def test_dtd_and_entities_are_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><r>&b;</r>'
        with self.assertRaises(ValueError):
            safe_xml.fromstring(bomb)
        with self.assertRaises(ValueError):
            list(safe_xml.iterparse(io.BytesIO(bomb)))
        with self.assertRaises(ValueError):
            safe_xml.fromstring(b'<!doctype r SYSTEM "file:///etc/passwd"><r/>')

    def test_declaration_split_across_reads_is_detected(self):
        class Trickle(io.BytesIO):
            def read(self, size=-1):
                return super().read(3)

        with self.assertRaises(ValueError):
            list(safe_xml.iterparse(Trickle(b'<!DOCTYPE r [<!ENTITY a "x">]><r>&a;</r>')))


class CredentialsKeyTests(unittest.TestCase):
    def test_default_keeps_plaintext_key_compatible(self):
        with tempfile.TemporaryDirectory() as folder:
            first = credentials_key.load(folder)
            Fernet(first)
            self.assertEqual((Path(folder) / "credentials.key").read_bytes(), first)
            self.assertEqual(credentials_key.load(folder), first)
            self.assertFalse((Path(folder) / "credentials.key.dpapi").exists())

    def test_enabled_migration_removes_plaintext_after_verified_copy(self):
        def fake(value, decode=False):
            return value[::-1]  # reversible stand-in for DPAPI on non-Windows hosts

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"ECMP_CREDENTIALS_DPAPI": "1"}),
            patch("backend.dpapi.available", return_value=True),
            patch("backend.dpapi.protect", side_effect=fake),
        ):
            plain = Path(folder) / "credentials.key"
            key = Fernet.generate_key()
            plain.write_bytes(key)
            self.assertEqual(credentials_key.load(folder), key)
            self.assertFalse(plain.exists())
            self.assertTrue((Path(folder) / "credentials.key.dpapi").exists())
            self.assertEqual(credentials_key.load(folder), key)

    def test_failed_verification_keeps_plaintext(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"ECMP_CREDENTIALS_DPAPI": "1"}),
            patch("backend.dpapi.available", return_value=True),
            patch("backend.dpapi.protect", side_effect=lambda v, decode=False: "broken" if decode else v),
        ):
            plain = Path(folder) / "credentials.key"
            plain.write_bytes(Fernet.generate_key())
            with self.assertRaises(RuntimeError):
                credentials_key.load(folder)
            self.assertTrue(plain.exists())


class OverviewAccessTests(unittest.TestCase):
    def test_old_public_endpoint_removed_and_snapshot_requires_auth(self):
        from fastapi import Header, HTTPException
        app = FastAPI()
        service = type('S', (), {'store': type('Store', (), {'list': staticmethod(lambda: [])})()})()
        def admin(authorization: str = Header(default='')):
            if authorization != 'Bearer ' + 't' * 32:
                raise HTTPException(401)
        overview_snapshot.install(app, service, admin)
        with TestClient(app) as client, patch.object(overview_snapshot.OverviewCache, 'read', return_value={'ok': True}):
            self.assertEqual(client.get('/api/public/overview').status_code, 404)
            self.assertEqual(client.get('/api/home/snapshot').status_code, 401)
            self.assertEqual(client.get('/api/home/snapshot', headers={'Authorization': 'Bearer ' + 't' * 32}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
