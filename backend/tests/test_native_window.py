import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, patch
from backend.native_window import WindowController
from backend.browser import Session


class FakeNative:
    def __init__(self):
        self.identities = {11: (11, 1, "chrome.exe")}
        self.frames = {11: [{"handle": 21, "visible": True, "minimized": False}]}
        self.changes = []

    def identity(self, pid):
        return self.identities.get(pid)

    def windows(self, pid):
        return [dict(w) for w in self.frames.get(pid, [])]

    def change(self, handle, visible, activate=True):
        self.changes.append((handle, visible))
        for windows in self.frames.values():
            for window in windows:
                if window["handle"] == handle:
                    window.update(visible=visible, minimized=False)


class WindowTests(unittest.TestCase):
    def controller(self):
        native = FakeNative()
        discover = Mock(return_value=11)
        return WindowController(9222, Path("profiles/1"), native, discover), native, discover

    def test_repeated_passive_shows_do_not_launch_locator_or_touch_visible_window(self):
        control, native, discover = self.controller()
        for _ in range(8):
            self.assertEqual(control.apply(True, activate=False)["changed"], 0)
        discover.assert_called_once()
        self.assertFalse(native.changes)

    def test_manual_show_activates_an_already_visible_window(self):
        control, native, _ = self.controller()
        self.assertEqual(control.apply(True)["changed"], 1)
        self.assertEqual(native.changes, [(21, True)])

    def test_new_work_window_guard_does_not_show_hide_or_activate(self):
        control, native, _ = self.controller()
        native.protect = Mock()
        native.frames[11].append({"handle": 22, "visible": False, "minimized": False})
        control.guard()
        self.assertEqual(native.protect.call_args_list, [unittest.mock.call(21, False), unittest.mock.call(22, False)])
        self.assertFalse(native.changes)

    @unittest.skipUnless(__import__("os").name == "nt", "Windows startup flags")
    def test_cold_launch_requests_no_activation(self):
        from backend.native_window import passive_launch_options

        startup = passive_launch_options()["startupinfo"]
        self.assertTrue(startup.dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(startup.wShowWindow, 4)

    def test_external_hide_minimize_and_new_window_are_detected_not_cached_as_visible(self):
        control, native, discover = self.controller()
        control.apply(True, activate=False)
        native.frames[11][0]["visible"] = False
        self.assertEqual(control.apply(True, activate=False)["changed"], 1)
        native.frames[11][0]["minimized"] = True
        self.assertEqual(control.apply(True, activate=False)["changed"], 1)
        native.frames[11].append({"handle": 22, "visible": False, "minimized": False})
        self.assertEqual(control.apply(True, activate=False)["changed"], 1)
        discover.assert_called_once()

    def test_hide_is_idempotent_and_readonly_query_never_changes_windows(self):
        control, native, _ = self.controller()
        self.assertFalse(control.apply(False)["visible"])
        self.assertEqual(control.apply(False)["changed"], 0)
        self.assertFalse(control.apply()["visible"])
        self.assertEqual(native.changes, [(21, False)])

    def test_pid_reuse_relocates_by_shop_instead_of_controlling_an_unrelated_process(self):
        control, native, discover = self.controller()
        control.apply()
        native.identities[11] = (11, 999, "chrome.exe")
        native.identities[12] = (12, 2, "chrome.exe")
        native.frames[12] = [{"handle": 31, "visible": False, "minimized": False}]
        discover.return_value = 12
        control.apply(True)
        self.assertEqual(native.changes, [(31, True)])
        self.assertEqual(discover.call_count, 2)

    def test_ambiguous_identity_never_mutates_or_uses_last_cached_pid(self):
        control, native, discover = self.controller()
        control.apply()
        native.identities.clear()
        discover.side_effect = ValueError("not unique")
        with self.assertRaisesRegex(ValueError, "not unique"):
            control.apply(False)
        self.assertFalse(native.changes)
        self.assertIsNone(control.cached)

    def test_live_owned_process_skips_expensive_discovery_and_foreign_profile_does_not(self):
        control, _, discover = self.controller()
        process = SimpleNamespace(
            pid=11,
            poll=lambda: None,
            args=["chrome.exe", "--remote-debugging-port=9222", "--user-data-dir=" + str(Path("profiles/1").resolve())],
        )
        control.apply(True, process)
        discover.assert_not_called()
        control.invalidate()
        process.args[-1] = "--user-data-dir=" + str(Path("another-profile").resolve())
        control.apply(True, process)
        discover.assert_called_once()


@unittest.skipUnless(__import__("os").name == "nt", "Windows extended window styles")
class ActivationGuardTests(unittest.TestCase):
    def test_protection_preserves_other_flags_and_only_manual_show_clears_it(self):
        from backend.native_window import NativeWindows

        native = object.__new__(NativeWindows)
        style = [0x100]

        def change(handle, index, selected):
            before = style[0]
            style[0] = selected
            return before

        native.user = SimpleNamespace(
            GetWindowLongPtrW=Mock(side_effect=lambda *args: style[0]),
            SetWindowLongPtrW=Mock(side_effect=change),
            SetWindowPos=Mock(return_value=1),
            SetForegroundWindow=Mock(),
        )
        native.protect(21)
        self.assertEqual(style[0], 0x08000100)
        native.protect(21)
        self.assertEqual(native.user.SetWindowLongPtrW.call_count, 1)
        native.protect(21, True)
        self.assertEqual(style[0], 0x100)
        native.user.SetForegroundWindow.assert_not_called()
        for call in native.user.SetWindowPos.call_args_list:
            self.assertTrue(call.args[-1] & 0x10)


class VisibilitySemanticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_window_action_does_not_overwrite_manual_preference(self):
        session = SimpleNamespace(
            retired=False,
            headless=False,
            port=9222,
            profile=Path("profile"),
            process=None,
            manual_visibility=False,
            visible=False,
            window_controller=SimpleNamespace(apply=Mock(return_value={"visible": True})),
        )
        with patch("backend.browser.os.name", "nt"):
            await Session.window(session, True)
        self.assertTrue(session.visible)
        self.assertIs(session.manual_visibility, False)
        session.window_controller.apply.assert_called_once_with(True, None)

    async def test_login_auto_window_respects_explicit_hide_and_remote_control(self):
        session = SimpleNamespace(manual_visibility=False, window=AsyncMock())
        await Session.auto_window(session, True)
        session.window.assert_not_awaited()
        session.manual_visibility = None
        session.remote_control_active = True
        await Session.auto_window(session, True)
        session.window.assert_not_awaited()
