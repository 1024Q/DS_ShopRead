"""Release preflight tests use temporary files; never sign or commit this repo."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from contextlib import ExitStack

spec = importlib.util.spec_from_file_location(
    "release_update", Path(__file__).resolve().parents[1] / "scripts/release-update.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseChecks(unittest.TestCase):
    def pipeline(self, fail=False):
        events = []
        publisher = MagicMock()
        publisher.dirty_files.return_value = []

        def tests():
            events.append("tests")
            if fail:
                raise ValueError("test failed")

        publisher.run_tests.side_effect = tests
        publisher.publish.side_effect = lambda **kw: events.append("publish")
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(sys, "argv", ["release-update.py", "--message", "test", "--paths", "backend/a.py"])
            )
            stack.enter_context(
                patch.object(release.importlib.util, "spec_from_file_location", return_value=MagicMock())
            )
            stack.enter_context(patch.object(release.importlib.util, "module_from_spec", return_value=publisher))
            stack.enter_context(patch.object(release, "changed", return_value=["backend/a.py"]))
            stack.enter_context(patch.object(release, "staged_guard"))
            stack.enter_context(patch.object(release, "fingerprint", return_value=(b"head", {"a": "hash"})))
            stack.enter_context(patch.object(release, "git", return_value=b"author"))
            stack.enter_context(
                patch.object(
                    release.subprocess,
                    "run",
                    side_effect=lambda args, **kw: events.append("commit" if "commit" in args else "frontend"),
                )
            )
            stack.enter_context(patch.dict(release.os.environ))
            if fail:
                with self.assertRaisesRegex(ValueError, "test failed"):
                    release.main()
                publisher.publish.assert_not_called()
                self.assertEqual(events, ["tests"])
            else:
                release.main()
                publisher.publish.assert_called_once_with(skip_tests=True, node_id=None)
                self.assertEqual(events, ["tests", "frontend", "commit", "publish"])

    def test_failed_test_never_commits_or_publishes(self):
        self.pipeline(fail=True)

    def test_publication_follows_successful_tests_and_commit(self):
        self.pipeline()

    def test_local_data_and_keys_cannot_be_selected(self):
        for name in (
            "../outside.py",
            ".data/center/config.json",
            ".browsers/profile",
            "key.pem",
            ".env.local",
            "collector-version.json",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.safe_path(name)
        self.assertEqual(release.safe_path("backend/credentials_key.py"), "backend/credentials_key.py")

    def test_symlink_outside_workspace_is_rejected(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            with patch.object(release, "ROOT", Path(root)):
                try:
                    (Path(root) / "escape").symlink_to(outside, target_is_directory=True)
                except OSError:
                    self.skipTest("Symlink permission unavailable")
                with self.assertRaises(ValueError):
                    release.safe_path("escape/file.py")

    def test_unrelated_staged_files_block_commit(self):
        with patch.object(release, "git", return_value=b"backend/a.py\0other.txt\0"):
            with self.assertRaises(ValueError):
                release.staged_guard(["backend/a.py"])

    def test_deletions_and_spaces_are_selected_but_conflicts_rejected(self):
        with patch.object(release, "git", return_value=b" D backend/old.py\0?? scripts/new file.py\0"):
            self.assertEqual(release.changed(["backend", "scripts"]), ["backend/old.py", "scripts/new file.py"])
        with patch.object(release, "git", return_value=b"UU backend/conflict.py\0"):
            with self.assertRaises(ValueError):
                release.changed(["backend"])


if __name__ == "__main__":
    unittest.main()
