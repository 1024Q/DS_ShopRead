import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import zipfile
from unittest.mock import Mock, patch
import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from backend import collector_update
from backend.collector_update import (
    canonical,
    verify,
    unpack,
    apply,
    rollback,
    update,
    destination,
    write_json,
    wait_ready,
    read_json,
    probation,
    recover,
    startup,
    prune,
    compatible,
    retry_delay,
)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.key = Ed25519PrivateKey.generate()
        (self.root / "collector-update-public.pem").write_bytes(
            self.key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
        (self.root / "collector-launcher.py").write_text("# old code")
        (self.root / ".data").mkdir()
        (self.root / ".data/api.key").write_text("local-only")
        (self.root / ".data/account.txt").write_text("must survive")
        self.files = {
            "collector-launcher.py": b"# new code",
            "collector-version.json": canonical(
                {
                    "version": "20261012-abc",
                    "requirements": "same",
                    "files": ["collector-launcher.py", "collector-version.json"],
                }
            ),
        }
        write_json(
            self.root / "collector-version.json",
            {
                "version": "20261011-abc",
                "requirements": "same",
                "files": ["collector-launcher.py", "collector-version.json"],
            },
        )
        self.zip = self.root / "code.zip"
        with zipfile.ZipFile(self.zip, "w") as z:
            for name, data in self.files.items():
                z.writestr(name, data)
        self.release = {
            "format": 1,
            "version": "20261012-abc",
            "requirements": "same",
            "sha256": hashlib.sha256(self.zip.read_bytes()).hexdigest(),
            "files": {n: hashlib.sha256(v).hexdigest() for n, v in self.files.items()},
        }
        self.envelope = {
            "release": self.release,
            "signature": base64.b64encode(self.key.sign(canonical(self.release))).decode(),
        }
        self.updates = self.root / ".data/updates"

    def tearDown(self):
        self.temp.cleanup()

    def code(self):
        return (self.root / "collector-launcher.py").read_text()

    def state(self):
        return read_json(self.updates / "status.json", {})

    def runner(self, **values):
        return Mock(
            **{
                "root": self.root,
                "port": 18081,
                "config": {"url": "https://center.test", "id": "node", "token": "secret"},
                "restarts": 0,
                "running": True,
                "children": {},
                **values,
            }
        )

    def test_signature_and_tamper(self):
        self.assertEqual(verify(self.root, self.envelope), self.release)
        self.envelope["release"]["version"] = "20261012-other"
        with self.assertRaises(Exception):
            verify(self.root, self.envelope)

    def test_paths_cannot_touch_data_runtime_or_escape(self):
        for name in (
            ".data/api.key",
            "runtime/python.exe",
            "../outside.py",
            "backend/../../outside.py",
            "backend\\evil.py",
            "C:/evil.py",
            "/backend/evil.py",
        ):
            with self.assertRaises(ValueError):
                destination(self.root, name)

    def test_install_and_crash_recovery_preserve_accounts(self):
        stage = self.root / "stage"
        unpack(self.root, self.zip, self.release, stage)
        apply(self.root, stage, self.release)
        self.assertEqual(self.code(), "# new code")
        rollback(self.root)
        self.assertEqual(self.code(), "# old code")
        self.assertEqual((self.root / ".data/account.txt").read_text(), "must survive")

    def test_archive_tampering_never_applies(self):
        self.zip.write_bytes(b"broken")
        with self.assertRaises(ValueError):
            unpack(self.root, self.zip, self.release, self.root / "stage")
        self.assertEqual(self.code(), "# old code")

    def run_update(self, busy=False, fail_start=False, runner=None, expect_error=False):
        runner = runner or self.runner()
        if fail_start:
            runner.start.side_effect = [ValueError("new version failed"), None]

        def respond(request):
            return (
                httpx.Response(200, json=self.envelope)
                if request.url.path.endswith("latest")
                else httpx.Response(200, content=self.zip.read_bytes())
            )

        client = httpx.Client(base_url="https://center.test", transport=httpx.MockTransport(respond))
        with (
            patch("backend.collector_update.httpx.Client", return_value=client),
            patch("backend.collector_update.httpx.get", return_value=httpx.Response(404)),
            patch(
                "backend.collector_update.httpx.post",
                return_value=httpx.Response(409 if busy else 200, request=httpx.Request("POST", "http://localhost/")),
            ),
            patch("backend.collector_update.time.sleep"),
        ):
            if expect_error:
                with (
                    patch("backend.collector_update.automatic_ready", return_value=True),
                    self.assertRaises(ValueError),
                ):
                    update(runner)
                return runner, None
            with patch("backend.collector_update.automatic_ready", return_value=True):
                result = update(runner)
        return runner, result

    def test_busy_collector_defers_without_stop(self):
        runner, result = self.run_update(busy=True)
        runner.stop.assert_not_called()
        self.assertEqual(self.code(), "# old code")

    def test_start_failure_rolls_back(self):
        runner, result = self.run_update(fail_start=True)
        self.assertEqual(self.code(), "# old code")
        self.assertEqual(runner.start.call_count, 2)
        self.assertEqual(self.state()["failed_version"], self.release["version"])
        self.assertEqual(list(self.updates.glob("download-*")), [])

    def test_success_restarts_and_commits_after_probation(self):
        runner, result = self.run_update()
        runner.stop.assert_called_once()
        runner.start.assert_called_once()
        self.assertEqual(self.code(), "# new code")
        self.assertTrue((self.updates / "journal.json").exists())
        self.assertEqual(list(self.updates.glob("download-*")), [])
        self.assertIn("试运行", update(runner))  # No new install while the previous one is on probation.
        with patch("backend.collector_update.healthy", return_value=True):
            self.assertIn("试运行", probation(runner, True, time.time()))
            self.assertTrue((self.updates / "journal.json").exists())
            with patch("backend.collector_update.time.time", return_value=time.time() + collector_update.PROBATION + 1):
                self.assertIn("自动升级成功", probation(runner))
        self.assertFalse((self.updates / "journal.json").exists())
        self.assertEqual(len(list(self.updates.glob("backup-*"))), 1)
        self.assertEqual(self.state()["version"], self.release["version"])
        self.assertTrue(self.state()["updated"])

    def test_probation_crash_rolls_back_and_marks_failed(self):
        runner, _ = self.run_update()
        runner.restarts = 1
        self.assertIn("已自动回退", probation(runner))
        self.assertEqual(self.code(), "# old code")
        self.assertFalse((self.updates / "journal.json").exists())
        self.assertEqual(self.state()["failed_version"], self.release["version"])

    def test_probation_waits_for_center_then_rolls_back(self):
        runner, _ = self.run_update()
        now = time.time()
        with patch("backend.collector_update.healthy", return_value=True):
            probation(runner, False, now + 1)
            with patch("backend.collector_update.time.time", return_value=now + collector_update.PROBATION + 5):
                self.assertIn("试运行", probation(runner))
            with patch(
                "backend.collector_update.time.time",
                return_value=now + collector_update.PROBATION + collector_update.PROBATION_GRACE + 5,
            ):
                self.assertIn("未能连接中心", probation(runner))
        self.assertEqual(self.code(), "# old code")

    def test_probation_ignores_center_observation_from_old_version(self):
        runner, _ = self.run_update()
        with patch("backend.collector_update.healthy", return_value=True):
            probation(runner, True, 1)
        self.assertNotIn("connected", self.state()["probation"])

    def test_probation_tolerates_transient_health_failures(self):
        runner, _ = self.run_update()
        now = time.time()
        with patch("backend.collector_update.healthy", return_value=False):
            for offset in (0, 15, 30):
                with patch("backend.collector_update.time.time", return_value=now + offset):
                    self.assertIn("试运行", probation(runner))
            self.assertTrue((self.updates / "journal.json").exists())
            with patch("backend.collector_update.time.time", return_value=now + 91):
                self.assertIn("已自动回退", probation(runner))

    def test_failed_version_is_not_repeated_as_new_error(self):
        runner, _ = self.run_update()
        runner.restarts = 1
        probation(runner)
        _, result = self.run_update(runner=runner)
        self.assertIsNone(result)

    def test_rollback_start_failure_surfaces_and_recovers(self):
        runner = self.runner(running=False)
        runner.start.side_effect = [ValueError("new version failed")] + [ValueError("old failed")] * 3 + [None]
        self.run_update(runner=runner, expect_error=True)
        self.assertTrue(self.state()["stopped"])
        self.assertEqual(self.code(), "# old code")
        with patch("backend.collector_update.time.sleep"):
            self.assertIn("采集服务已重新启动", recover(runner))
        self.assertNotIn("stopped", self.state())
        self.assertIsNone(recover(runner))

    def test_startup_rolls_back_unfinished_probation(self):
        self.run_update()
        startup(self.root)
        self.assertEqual(self.code(), "# old code")
        self.assertEqual(self.state()["failed_version"], self.release["version"])

    def test_prune_keeps_only_needed_backup(self):
        for name in ("download-a", "backup-old", "backup-new"):
            (self.updates / name).mkdir(parents=True)
        os.utime(self.updates / "backup-old", (1, 1))
        prune(self.updates)
        self.assertEqual(sorted(p.name for p in self.updates.iterdir()), ["backup-new"])
        (self.updates / "backup-old").mkdir()
        os.utime(self.updates / "backup-new", (1, 1))
        write_json(self.updates / "journal.json", {"backup": "backup-new", "files": {}})
        prune(self.updates)
        self.assertEqual(sorted(p.name for p in self.updates.iterdir()), ["backup-new", "journal.json"])

    def test_requirements_rollout_compatibility(self):
        (self.root / "backend").mkdir()
        (self.root / "backend/requirements.txt").write_bytes(b"httpx\n")
        local = hashlib.sha256(b"httpx\n").hexdigest()
        self.assertTrue(compatible(self.root, {"requirements": "legacy"}, {"requirements": "legacy"}))
        # Installed by an old release (no collector hash): a center-only bump still matches the local collector requirements.
        self.assertTrue(
            compatible(
                self.root,
                {"requirements": "legacy"},
                {"requirements": "center-bumped", "collector_requirements": local},
            )
        )
        self.assertTrue(
            compatible(
                self.root,
                {"requirements": "a", "collector_requirements": "c"},
                {"requirements": "b", "collector_requirements": "c"},
            )
        )
        self.assertFalse(
            compatible(
                self.root,
                {"requirements": "a", "collector_requirements": "c"},
                {"requirements": "b", "collector_requirements": "d"},
            )
        )
        self.assertFalse(compatible(self.root, {"requirements": "legacy"}, {"requirements": "other"}))
        self.assertTrue(compatible(self.root, {}, {"requirements": "x", "collector_requirements": local}))

    def test_deferral_backs_off_exponentially(self):
        delays = []
        for _ in range(4):
            self.run_update(busy=True)
            delays.append(retry_delay(self.root))
        self.assertEqual(delays, [60, 120, 240, 480])
        self.assertEqual(list(self.updates.glob("download-*")), [])

    def test_draining_polls_until_current_work_finishes(self):
        request = httpx.Request("POST", "http://localhost/")
        busy = httpx.Response(
            409, json={"detail": {"phase": "draining", "blockers": {"collecting": 2}}}, request=request
        )
        ready = httpx.Response(200, json={"ready": True}, request=request)
        state = self.updates / "status.json"
        with (
            patch("backend.collector_update.httpx.get", return_value=httpx.Response(404)),
            patch("backend.collector_update.httpx.post", side_effect=[busy, busy, ready]) as post,
            patch("backend.collector_update.time.sleep") as sleep,
        ):
            self.assertTrue(wait_ready(Mock(port=8001), {}, state, {}, "new"))
            self.assertEqual(post.call_count, 3)
            self.assertEqual(sleep.call_count, 2)
        self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["blockers"]["collecting"], 2)

    def test_draining_timeout_is_bounded(self):
        response = httpx.Response(
            409, json={"detail": {"phase": "draining"}}, request=httpx.Request("POST", "http://localhost/")
        )
        clock = [0.0]
        with (
            patch("backend.collector_update.httpx.get", return_value=httpx.Response(404)),
            patch("backend.collector_update.httpx.post", return_value=response) as post,
            patch("backend.collector_update.time.sleep", side_effect=lambda s: clock.__setitem__(0, clock[0] + s)),
            patch("backend.collector_update.time.monotonic", side_effect=lambda: clock[0]),
        ):
            self.assertFalse(wait_ready(Mock(port=8001), {}, self.updates / "status.json", {}, "new"))
            self.assertLessEqual(post.call_count, collector_update.DRAIN_SECONDS // 2 + 1)
            self.assertLessEqual(clock[0], collector_update.DRAIN_SECONDS)

    def test_active_remote_control_never_engages_gate(self):
        probe = httpx.Response(200, json={"blockers": {"collecting": 0, "busy_windows": 1, "remote_controls": 1}})
        with (
            patch("backend.collector_update.httpx.get", return_value=probe),
            patch("backend.collector_update.httpx.post") as post,
        ):
            self.assertFalse(wait_ready(Mock(port=8001), {}, self.updates / "status.json", {}, "new"))
            post.assert_not_called()
        self.assertEqual(self.state()["phase"], "waiting")

    def test_remote_control_during_drain_yields_immediately(self):
        busy = httpx.Response(
            409,
            json={"detail": {"phase": "draining", "blockers": {"collecting": 0, "remote_controls": 1}}},
            request=httpx.Request("POST", "http://localhost/"),
        )
        with (
            patch("backend.collector_update.httpx.get", return_value=httpx.Response(404)),
            patch("backend.collector_update.httpx.post", return_value=busy) as post,
            patch("backend.collector_update.time.sleep") as sleep,
        ):
            self.assertFalse(wait_ready(Mock(port=8001), {}, self.updates / "status.json", {}, "new"))
            self.assertEqual(post.call_count, 1)
            sleep.assert_not_called()

    def test_ui_handoff_does_not_start_collector_twice_or_rollback(self):
        runner = self.runner(ui_handoff=True)
        self.run_update(runner=runner)
        runner.stop.assert_called_once()
        runner.start.assert_not_called()
        self.assertTrue(runner.restart_requested)
        startup(self.root)
        self.assertEqual(self.code(), "# new code")
        self.assertTrue(self.state()["resume_probation"])
        self.assertFalse((self.updates / "handoff.json").exists())
        collector_update.resume_probation(runner)
        self.assertNotIn("resume_probation", self.state())
        with (
            patch("backend.collector_update.healthy", return_value=True),
            patch("backend.collector_update.time.time", return_value=time.time() + collector_update.PROBATION + 1),
        ):
            probation(runner, True, time.time() + 1)
        self.assertEqual(self.state()["ui_restart_version"], self.release["version"])

    def test_backend_only_update_also_restarts_launcher_once(self):
        runner = self.runner(ui_handoff=True)
        with patch("backend.collector_restart.fingerprint", return_value="unchanged-ui"):
            self.run_update(runner=runner)
        runner.stop.assert_called_once()
        runner.start.assert_not_called()
        self.assertTrue(runner.restart_requested)
        self.assertEqual(self.state()["ui_restart_version"], self.release["version"])
        startup(self.root)
        collector_update.resume_probation(runner)
        with (
            patch("backend.collector_update.healthy", return_value=True),
            patch("backend.collector_update.time.time", return_value=time.time() + collector_update.PROBATION + 1),
        ):
            probation(runner, True, time.time() + 1)
        self.assertFalse((self.updates / "journal.json").exists())
        self.assertFalse(collector_update.probation(runner))

    def test_wait_keeps_stable_release_timer(self):
        write_json(self.updates / "status.json", {"pending_version": "new", "pending_since": 100, "updated": 1})
        with patch(
            "backend.collector_update.httpx.get",
            return_value=httpx.Response(200, json={"blockers": {"remote_controls": 1}}),
        ):
            wait_ready(self.runner(), {}, self.updates / "status.json", {}, "new")
        self.assertEqual(self.state()["pending_since"], 100)
        self.assertTrue(collector_update.automatic_ready(self.updates / "status.json", {}, {"version": "new"}, 1000))

    def test_repeated_automatic_check_same_version_is_silent(self):
        runner, _ = self.run_update()
        with (
            patch("backend.collector_update.healthy", return_value=True),
            patch("backend.collector_update.time.time", return_value=time.time() + collector_update.PROBATION + 1),
        ):
            probation(runner)
        runner.reset_mock()
        _, result = self.run_update(runner=runner)
        self.assertIsNone(result)
        runner.stop.assert_not_called()
        runner.start.assert_not_called()
