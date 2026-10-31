import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/publish-collector-update.py"
spec = importlib.util.spec_from_file_location("publish_collector_update", SCRIPT)
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = Path(self.temp.name) / "center/update-signing.local.pem"
        self.env = patch.dict(os.environ)
        self.env.start()
        os.environ.pop(publisher.PASSPHRASE, None)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_missing_key_is_a_hard_error(self):
        with self.assertRaises(publisher.PublishError):
            publisher.load_key(self.key)
        self.assertFalse(self.key.exists())

    def test_init_key_never_overwrites(self):
        key = publisher.load_key(self.key, init_key=True)
        self.assertTrue(self.key.exists())
        with self.assertRaises(publisher.PublishError):
            publisher.load_key(self.key, init_key=True)
        self.assertEqual(
            publisher.load_key(self.key).public_key().public_bytes_raw(), key.public_key().public_bytes_raw()
        )

    def test_encrypted_key_requires_passphrase(self):
        os.environ[publisher.PASSPHRASE] = "correct horse"
        key = publisher.load_key(self.key, init_key=True)
        self.assertIn(b"ENCRYPTED", self.key.read_bytes())
        self.assertEqual(
            publisher.load_key(self.key).public_key().public_bytes_raw(), key.public_key().public_bytes_raw()
        )
        os.environ[publisher.PASSPHRASE] = "wrong"
        with self.assertRaises(publisher.PublishError):
            publisher.load_key(self.key)
        del os.environ[publisher.PASSPHRASE]
        with self.assertRaises(publisher.PublishError):
            publisher.load_key(self.key)

    def test_legacy_unencrypted_key_still_loads_with_passphrase_set(self):
        key = Ed25519PrivateKey.generate()
        self.key.parent.mkdir(parents=True)
        self.key.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        self.assertEqual(
            publisher.load_key(self.key).public_key().public_bytes_raw(), key.public_key().public_bytes_raw()
        )
        os.environ[publisher.PASSPHRASE] = "set"
        self.assertEqual(
            publisher.load_key(self.key).public_key().public_bytes_raw(), key.public_key().public_bytes_raw()
        )

    def test_dirty_files_only_lists_packaged_paths(self):
        output = b"\0".join(
            [
                b" M backend/main.py",
                b"?? backend/new.py",
                b" M backend/tests/test_x.py",
                b"R  backend/renamed.py",
                b"backend/old.py",
                b" M config/a.json",
                b" M backend/notes.txt",
                b"",
            ]
        )
        with patch.object(publisher.subprocess, "run", return_value=Mock(stdout=output)):
            self.assertEqual(
                publisher.dirty_files(), ["backend/main.py", "backend/new.py", "backend/renamed.py", "config/a.json"]
            )

    def test_dirty_tree_refuses_before_tests(self):
        with (
            patch.object(publisher, "load_key"),
            patch.object(publisher, "dirty_files", return_value=["backend/main.py"]),
            patch.object(publisher, "run_tests") as tests,
        ):
            with self.assertRaises(publisher.PublishError):
                publisher.publish()
            tests.assert_not_called()

    def test_failing_tests_refuse(self):
        with patch.object(publisher.subprocess, "run", return_value=Mock(returncode=1)):
            with self.assertRaises(publisher.PublishError):
                publisher.run_tests()

    def test_release_tests_isolate_default_node_store(self):
        def run(*args, **kwargs):
            directory = Path(kwargs["env"]["PDD_DATA_DIR"])
            self.assertTrue(directory.is_dir())
            self.assertNotEqual(directory, publisher.ROOT / ".data")
            self.assertEqual(args[0][-2:], ["-s", "backend/tests"])
            return Mock(returncode=0)

        with patch.object(publisher.subprocess, "run", side_effect=run):
            publisher.run_tests()

    def test_requirement_hashes_keep_legacy_formula(self):
        legacy, collector = publisher.requirement_hashes()
        root = publisher.ROOT
        own = (root / "backend/requirements.txt").read_bytes()
        center = root / "backend/center/requirements.txt"
        self.assertEqual(collector, hashlib.sha256(own).hexdigest())
        self.assertEqual(legacy, hashlib.sha256(own + (center.read_bytes() if center.exists() else b"")).hexdigest())


if __name__ == "__main__":
    unittest.main()
