import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from backend.collector_restart import fingerprint, needed, prepare, launch
from backend.collector_update import write_json, read_json


class RestartTests(unittest.TestCase):
    def test_successful_process_start_clears_stale_restart_message(self):
        from backend.collector_launcher import Runner

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / ".data/updates/status.json"
            write_json(state, {"version": "v2", "message": "界面更新完成，正在自动重启"})
            runner = Runner(root)

            def spawn(kind):
                runner.children[kind] = Mock(poll=Mock(return_value=None))

            with (
                patch.object(runner, "occupied", return_value=False),
                patch.object(runner, "spawn", side_effect=spawn),
                patch("backend.collector_launcher.httpx.get", return_value=Mock(is_success=True)),
                patch("backend.collector_launcher.time.sleep"),
            ):
                runner.start({})
            self.assertTrue(runner.running)
            self.assertEqual(read_json(state)["message"], "自动升级成功")
            self.assertGreater(read_json(state)["restarted_at"], 0)

    def test_only_committed_ui_changes_restart_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "backend").mkdir()
            ui = root / "backend/collector_launcher.py"
            ui.write_text("old")
            loaded = fingerprint(root)
            write_json(root / "collector-version.json", {"version": "v2"})
            write_json(root / ".data/updates/status.json", {"version": "v2", "updated": 123})
            (root / "backend/business.py").write_text("business change")
            self.assertFalse(needed(root, loaded))
            ui.write_text("new")
            self.assertTrue(needed(root, loaded))
            write_json(root / ".data/updates/journal.json", {})
            self.assertFalse(needed(root, loaded))
            (root / ".data/updates/journal.json").unlink()
            self.assertFalse(needed(root, fingerprint(root)))

    def test_busy_restart_keeps_retry_marker_and_releases_pause(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "runtime").mkdir()
            (root / "runtime/pythonw.exe").touch()
            (root / ".data").mkdir()
            (root / ".data/api.key").write_text("test")
            write_json(root / "collector-version.json", {"version": "v2"})
            state = root / ".data/updates/status.json"
            write_json(state, {"version": "v2", "updated": 123})
            with (
                patch("backend.collector_restart.wait_ready", return_value=False),
                patch("backend.collector_restart.httpx.post") as cancel,
            ):
                with self.assertRaises(ValueError):
                    prepare(Mock(root=root, port=8001))
                cancel.assert_called_once()
                self.assertEqual(read_json(state)["updated"], 123)

    def test_relaunch_preserves_minimized_state_without_credentials(self):
        with (
            patch("backend.collector_restart.executable", return_value=Path("D:/collector/runtime/pythonw.exe")),
            patch("backend.collector_restart.subprocess.Popen") as popen,
        ):
            launch(Path("D:/collector"), True)
            args = popen.call_args.args[0]
            self.assertEqual(args[-2:], ["--auto", "--minimized"])
            self.assertEqual(len(args), 4)

    def test_updater_change_reloads_launcher(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "backend").mkdir()
            updater = root / "backend/collector_update.py"
            updater.write_text("old")
            loaded = fingerprint(root)
            updater.write_text("new")
            self.assertNotEqual(fingerprint(root), loaded)

    def test_same_version_cannot_restart_ui_again(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "collector-version.json", {"version": "v2"})
            write_json(
                root / ".data/updates/status.json", {"version": "v2", "updated": 123, "ui_restart_version": "v2"}
            )
            self.assertFalse(needed(root, "different-fingerprint"))
            write_json(root / "collector-version.json", {"version": "v3"})
            write_json(
                root / ".data/updates/status.json", {"version": "v3", "updated": 124, "ui_restart_version": "v2"}
            )
            self.assertTrue(needed(root, "different-fingerprint"))

    def test_stable_release_and_post_upgrade_cooldown(self):
        from backend.collector_update import automatic_ready

        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "status.json"
            write_json(state, {"updated": 1000})
            self.assertFalse(automatic_ready(state, {"version": "v1"}, {"version": "v2"}, 1100))
            self.assertFalse(automatic_ready(state, {"version": "v1"}, {"version": "v3"}, 1200))
            self.assertFalse(automatic_ready(state, {"version": "v1"}, {"version": "v3"}, 1390))
            self.assertTrue(automatic_ready(state, {"version": "v1"}, {"version": "v3"}, 1601))

    def test_local_runtime_fallback(self):
        from backend.collector_runtime import executable

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / ".runtime/collector"
            runtime.mkdir(parents=True)
            (runtime / "python.exe").touch()
            (runtime / "pythonw.exe").touch()
            self.assertEqual(executable(root), runtime / "python.exe")
            self.assertEqual(executable(root, True), runtime / "pythonw.exe")
