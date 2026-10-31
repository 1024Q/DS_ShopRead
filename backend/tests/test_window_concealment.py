import ctypes
import unittest
from ctypes import wintypes
from types import SimpleNamespace
from unittest.mock import Mock

from backend.native_window import NativeWindows, WindowController


class Desktop:
    def __init__(self):
        self.bounds = (100, 120, 1300, 970)
        self.style = 0x00040000 | 0x08000000
        self.moves = []

    def GetWindowRect(self, handle, pointer):
        rect = ctypes.cast(pointer, ctypes.POINTER(wintypes.RECT)).contents
        rect.left, rect.top, rect.right, rect.bottom = self.bounds
        return True

    def GetSystemMetrics(self, index):
        return {76: -1920, 77: -200, 78: 3840, 79: 1280}[index]

    def GetWindowLongPtrW(self, handle, index):
        return self.style

    def SetWindowLongPtrW(self, handle, index, value):
        previous = self.style
        self.style = value
        return previous

    def SetWindowPos(self, handle, after, x, y, width, height, flags):
        self.bounds = (x, y, x + width, y + height)
        self.moves.append((x, y, width, height, flags))
        return True


@unittest.skipUnless(__import__('os').name == 'nt', 'Windows window concealment')
class ConcealmentTests(unittest.TestCase):
    def native(self):
        native = NativeWindows.__new__(NativeWindows)
        native.user = Desktop()
        native.hidden_placements = {}
        return native

    def test_hide_stays_outside_all_monitors_even_if_chrome_reshows_hwnd(self):
        native = self.native()
        native.placement(7, False)
        self.assertLess(native.user.bounds[2], -1920)
        self.assertLess(native.user.bounds[3], -200)
        self.assertTrue(native.user.style & 0x80)
        self.assertFalse(native.user.style & 0x40000)
        self.assertTrue(native.user.moves[-1][-1] & 0x80)
        self.assertTrue(native.user.moves[-1][-1] & 0x10)

    def test_repeated_hide_retains_manual_restore_rectangle_and_styles(self):
        native = self.native()
        original = native.user.bounds
        native.placement(7, False)
        native.placement(7, False)
        native.placement(7, True)
        self.assertEqual(native.user.bounds, original)
        self.assertTrue(native.user.style & 0x40000)
        self.assertFalse(native.user.style & 0x80)
        self.assertFalse(native.hidden_placements)

    def test_new_service_can_restore_browser_saved_outside_desktop(self):
        native = self.native()
        native.user.bounds = (-3400, -1200, -2200, -350)
        native.user.style = 0x80 | 0x08000000
        native.placement(7, True)
        self.assertGreater(native.user.bounds[2], -1920)
        self.assertGreater(native.user.bounds[3], -200)
        self.assertTrue(native.user.style & 0x40000)

    def test_controller_uses_concealment_on_hide_and_restore_on_manual_show(self):
        window = {"handle": 7, "visible": False, "minimized": False}
        native = SimpleNamespace(identity=lambda pid: (pid, 1),
                                 windows=lambda pid: [window], protect=Mock(), placement=Mock(),
                                 change=Mock(side_effect=lambda handle, visible: window.update(visible=visible)))
        controller = WindowController(9222, 'profile', native=native, discover=lambda *a: 11)
        controller.apply(False)
        native.placement.assert_called_with(7, False)
        controller.apply(True)
        native.placement.assert_called_with(7, True)
        native.protect.assert_called_with(7, True)
        count = native.placement.call_count
        controller.apply()
        self.assertEqual(native.placement.call_count, count)


if __name__ == '__main__':
    unittest.main()
