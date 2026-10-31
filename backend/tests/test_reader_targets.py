import asyncio
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend import reader_targets as targets


class ReaderTargetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.session = self.session_for(1)
        self.browser = "ws://127.0.0.1:9222/devtools/browser/11111111-1111-4111-8111-111111111111"
        self.other_browser = "ws://127.0.0.1:9222/devtools/browser/22222222-2222-4222-8222-222222222222"
        self.lease = self.enterContext(patch.object(targets.collector_lease, "owns", return_value=True, create=True))
        self.enterContext(patch.dict(targets.process_status.IDENTITY, {"boot_id": "current-boot", "pid": 1234}))

    def session_for(self, sid, version=0):
        return SimpleNamespace(
            store=SimpleNamespace(directory=self.directory),
            shop={"id": sid},
            profile=self.directory / "profiles" / str(sid) / str(version),
        )

    def records(self, sid=1):
        path = self.directory / "reader-targets" / f"{sid}.json"
        return json.loads(path.read_text())["targets"] if path.exists() else []

    def old(self, name, session=None, browser=None):
        with patch.dict(targets.process_status.IDENTITY, {"boot_id": "old-boot"}):
            return targets.register(session or self.session, browser or self.browser, name)

    async def test_only_registered_old_targets_are_closed(self):
        old = self.old("owned-page")
        missing = self.old("already-closed")
        close = AsyncMock()
        result = await targets.cleanup(self.session, self.browser, {"owned-page", "home", "user-extra"}, close)
        self.assertEqual(result, {"owned-page"})
        close.assert_awaited_once_with("owned-page")
        self.assertEqual(self.records(), [])
        self.assertNotIn(str(self.session.profile), json.dumps(old))

    async def test_current_boot_is_protected_after_session_recreation_and_pid_reuse(self):
        current = targets.register(self.session, self.browser, "active")
        self.old("old-with-same-pid")
        replacement = self.session_for(1)
        close = AsyncMock()
        result = await targets.cleanup(replacement, self.browser, {"active", "old-with-same-pid"}, close)
        self.assertEqual(result, {"old-with-same-pid"})
        self.assertEqual(self.records(), [current])
        close.assert_awaited_once_with("old-with-same-pid")

    async def test_shops_and_profiles_are_separate(self):
        same_shop_other_profile = self.session_for(1, version=1)
        other_shop = self.session_for(2)
        other_profile_record = self.old("other-profile", same_shop_other_profile)
        other_shop_record = self.old("other-shop", other_shop)
        self.old("this-profile")
        close = AsyncMock()
        await targets.cleanup(self.session, self.browser, {"this-profile", "other-profile", "other-shop"}, close)
        close.assert_awaited_once_with("this-profile")
        self.assertEqual(self.records(), [other_profile_record])
        self.assertEqual(self.records(2), [other_shop_record])

    async def test_changed_browser_uuid_drops_only_old_record_without_closing(self):
        self.old("reused-target-id", browser=self.other_browser)
        current = targets.register(self.session, self.browser, "reused-target-id")
        close = AsyncMock()
        self.assertEqual(await targets.cleanup(self.session, self.browser, {"reused-target-id"}, close), set())
        close.assert_not_awaited()
        self.assertEqual(self.records(), [current])

    async def test_close_timeout_keeps_record_for_retry(self):
        old = self.old("retry-page")
        close = AsyncMock(side_effect=TimeoutError())
        with self.assertLogs(targets.__name__, level="WARNING"):
            self.assertEqual(await targets.cleanup(self.session, self.browser, {"retry-page"}, close), set())
        self.assertEqual(self.records(), [old])
        close.side_effect = None
        self.assertEqual(await targets.cleanup(self.session, self.browser, {"retry-page"}, close), {"retry-page"})
        self.assertEqual(self.records(), [])

    async def test_new_registration_during_close_is_not_lost(self):
        self.old("old-page")
        added = []

        async def close(target):
            added.append(targets.register(self.session, self.browser, "new-page"))
            await asyncio.sleep(0)

        self.assertEqual(await targets.cleanup(self.session, self.browser, {"old-page"}, close), {"old-page"})
        self.assertEqual(self.records(), added)

    async def test_forget_does_not_remove_newer_claim_of_same_target(self):
        old = self.old("same-target")
        current = targets.register(self.session, self.browser, "same-target")
        targets.forget(self.session, old)
        self.assertEqual(self.records(), [current])

    async def test_no_lease_means_no_writes_or_closes(self):
        record = self.old("owned-page")
        self.lease.return_value = False
        self.assertIsNone(targets.register(self.session, self.browser, "new-page"))
        targets.forget(self.session, record)
        close = AsyncMock()
        self.assertEqual(await targets.cleanup(self.session, self.browser, {"owned-page"}, close), set())
        self.assertEqual(self.records(), [record])
        close.assert_not_awaited()

    async def test_lightweight_inventory_is_shop_profile_and_lease_scoped(self):
        self.assertFalse(targets.has_records(self.session))
        self.assertFalse((self.directory / "reader-targets").exists())
        different = self.session_for(1, version=1)
        self.old("different-profile", different)
        self.assertFalse(targets.has_records(self.session))
        self.assertTrue(targets.has_records(different))
        self.old("matching-profile")
        self.assertTrue(targets.has_records(self.session))
        self.lease.return_value = False
        self.assertFalse(targets.has_records(self.session))

    async def test_concurrent_registrations_preserve_all_records(self):
        names = [f"target-{i}" for i in range(8)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda name: targets.register(self.session, self.browser, name), names))
        self.assertEqual({r["target_id"] for r in self.records()}, set(names))

    async def test_lock_key_never_resolves_a_concurrently_replaced_target(self):
        path = self.directory / "reader-targets" / "1.json"
        with patch.object(Path, "resolve", side_effect=AssertionError("target resolution changes during replace")):
            first = targets._lock(path)
            self.assertIs(targets._lock(path), first)
            if os.name == "nt":
                self.assertIs(targets._lock(Path("\\\\?\\" + str(path.absolute()))), first)

    @unittest.skipUnless(os.name == "nt", "Windows extended-length path normalization")
    async def test_profile_identity_does_not_depend_on_extended_path_spelling(self):
        original = Path.resolve
        before = targets._scope(self.session)

        def prefixed(path, *args, **kwargs):
            resolved = str(original(path, *args, **kwargs))
            return Path(resolved if resolved.startswith("\\\\?\\") else "\\\\?\\" + resolved)

        with patch.object(Path, "resolve", prefixed):
            self.assertEqual(targets._scope(self.session), before)
        self.session.profile.mkdir(parents=True)
        self.assertEqual(targets._scope(self.session), before)

    async def test_missing_session_metadata_and_invalid_browser_never_close(self):
        close = AsyncMock()
        for session in (SimpleNamespace(), SimpleNamespace(store=self.session.store, shop={"id": 1})):
            self.assertIsNone(targets.register(session, self.browser, "new-page"))
            self.assertEqual(await targets.cleanup(session, self.browser, {"new-page"}, close), set())
        self.old("owned-page")
        self.assertEqual(
            await targets.cleanup(self.session, "ws://127.0.0.1:9222/devtools/page/wrong", {"owned-page"}, close), set()
        )
        close.assert_not_awaited()

    async def test_corrupt_ledger_is_preserved(self):
        path = self.directory / "reader-targets" / "1.json"
        path.parent.mkdir()
        path.write_text("{broken")
        close = AsyncMock()
        with self.assertLogs(targets.__name__, level="WARNING"):
            self.assertEqual(await targets.cleanup(self.session, self.browser, {"home"}, close), set())
        with self.assertRaises(ValueError):
            targets.register(self.session, self.browser, "new-page")
        self.assertEqual(path.read_text(), "{broken")
        close.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
