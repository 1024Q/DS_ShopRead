import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from backend.collector_launcher import center_url, protect, Runner, save_config, load_config


class LauncherTests(unittest.TestCase):
    def test_center_url_validation(self):
        self.assertEqual(center_url("https://example.com/"), "https://example.com")
        self.assertEqual(center_url("http://127.0.0.1:3211/"), "http://127.0.0.1:8010")
        self.assertEqual(center_url("http://172.16.22.200:3211/"), "http://172.16.22.200:8010")
        for value in ["http://192.168.1.10:8010", "http://10.0.0.10:8010", "http://[fd00::1]:8010"]:
            self.assertEqual(center_url(value), value)
        for value in [
            "http://example.com",
            "http://172.32.0.1:8010",
            "http://8.8.8.8:8010",
            "http://localhost.evil.com:8010",
            "http://192.168.1.1.evil.com",
            "https://a:b@example.com",
            "https://example.com/?token=secret",
            "file:///test",
        ]:
            with self.assertRaises(ValueError):
                center_url(value)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_protected_configuration_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            with (
                patch("backend.collector_launcher.DATA", Path(folder)),
                patch("backend.collector_launcher.CONFIG", path),
            ):
                config = {
                    "url": "https://example.com",
                    "id": "test",
                    "token": "secret-never-in-cleartext",
                    "auto": False,
                }
                save_config(config)
                self.assertNotIn(config["token"], path.read_text())
                self.assertEqual(load_config(), config)

    def test_occupied_port_never_attaches_or_kills_existing_service(self):
        runner = Runner()
        with patch.object(runner, "occupied", return_value=True), patch.object(runner, "spawn") as spawn:
            with self.assertRaises(ValueError):
                runner.start({})
            spawn.assert_not_called()

    def test_stop_only_owns_spawned_children(self):
        runner = Runner()
        process = Mock()
        process.poll.return_value = None
        runner.children = {"agent": process}
        runner.stop()
        process.terminate.assert_called_once()
        process.wait.assert_called_once()
        self.assertFalse(runner.running)
