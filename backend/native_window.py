"""Validated, process-local Chrome window control without a shell on every task."""

import ctypes
import json
import os
import subprocess
import threading
import time
from ctypes import wintypes
from .window_layout import matches_process


def passive_launch_options():
    """A cold browser start must not activate its first window either."""
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 4  # SW_SHOWNOACTIVATE
    return {"startupinfo": startup}


def background_target_bounds():
    """Set initial CDP window bounds before Chrome creates any native window."""
    if os.name != "nt":
        return {}
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetSystemMetrics.argtypes = [ctypes.c_int]
    user.GetSystemMetrics.restype = ctypes.c_int
    return {
        "left": user.GetSystemMetrics(76) - 1300,
        "top": user.GetSystemMetrics(77) - 950,
        "width": 1200,
        "height": 850,
        "windowState": "normal",
    }


class NativeWindows:
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.hidden_placements = {}
        signatures = {
            "EnumWindows": ([self.callback, wintypes.LPARAM], wintypes.BOOL),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
            "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "GetSystemMetrics": ([ctypes.c_int], ctypes.c_int),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsIconic": ([wintypes.HWND], wintypes.BOOL),
            "ShowWindowAsync": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            "SetForegroundWindow": ([wintypes.HWND], wintypes.BOOL),
            "GetWindowLongPtrW": ([wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t),
            "SetWindowLongPtrW": ([wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t),
            "SetWindowPos": ([wintypes.HWND, wintypes.HWND] + [ctypes.c_int] * 4 + [wintypes.UINT], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.user, name)
            function.argtypes = args
            function.restype = result
        signatures = {
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "GetProcessTimes": ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            "QueryFullProcessImageNameW": (
                [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)],
                wintypes.BOOL,
            ),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes = args
            function.restype = result

    def identity(self, pid):
        handle = self.kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not self.kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not self.kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                return None
            path = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(path))
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
                return None
            if os.path.basename(path.value).lower() != "chrome.exe":
                return None
            return (pid, times[0].dwHighDateTime, times[0].dwLowDateTime, os.path.normcase(path.value))
        finally:
            self.kernel.CloseHandle(handle)

    def windows(self, pid):
        result = []

        @self.callback
        def collect(handle, _):
            owner = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(handle, ctypes.byref(owner))
            if owner.value == pid:
                name = ctypes.create_unicode_buffer(256)
                self.user.GetClassNameW(handle, name, len(name))
                if name.value == "Chrome_WidgetWin_1" and self.user.GetWindowTextLengthW(handle) > 0:
                    result.append(
                        {
                            "handle": handle,
                            "visible": bool(self.user.IsWindowVisible(handle)),
                            "minimized": bool(self.user.IsIconic(handle)),
                        }
                    )
            return True

        if not self.user.EnumWindows(collect, 0):
            raise ValueError("无法枚举店铺浏览器窗口")
        return result

    def protect(self, handle, allow_activation=False):
        # Keep later page navigation / Chrome UI from activating a background
        # window. Only the explicit show action removes this window-level guard.
        style = self.user.GetWindowLongPtrW(handle, -20)  # GWL_EXSTYLE
        selected = style & ~0x08000000 if allow_activation else style | 0x08000000  # WS_EX_NOACTIVATE
        if selected != style:
            ctypes.set_last_error(0)
            if not self.user.SetWindowLongPtrW(handle, -20, selected) and ctypes.get_last_error():
                raise ValueError("无法设置店铺窗口后台保护")
            # Apply the changed frame flags without changing focus, position or size.
            if not self.user.SetWindowPos(handle, 0, 0, 0, 0, 0, 0x0037):
                raise ValueError("无法应用店铺窗口后台保护")

    def change(self, handle, visible, activate=True):
        # 9 = SW_RESTORE (show and activate); 4 = SW_SHOWNOACTIVATE keeps the user's
        # current window focused when a debug-mode collection shows its browser.
        if not self.user.ShowWindowAsync(handle, (9 if activate else 4) if visible else 0):
            raise ValueError("窗口显示隐藏失败")
        if visible and activate:
            self.user.SetForegroundWindow(handle)

    def placement(self, handle, visible):
        """Chrome can re-show a hidden HWND during CDP input/navigation.

        Keep silent windows outside the entire virtual desktop and out of the
        taskbar as well. Manual show restores the saved rectangle/styles first.
        No page dimensions, profile, login data or rendering mode are changed.
        """
        rect = wintypes.RECT()
        if not self.user.GetWindowRect(handle, ctypes.byref(rect)):
            raise ValueError("无法读取店铺窗口位置")
        vx, vy, vw, vh = (self.user.GetSystemMetrics(i) for i in (76, 77, 78, 79))
        if vw <= 0 or vh <= 0:
            raise ValueError("无法读取桌面显示范围")
        on_screen = rect.right > vx and rect.left < vx + vw and rect.bottom > vy and rect.top < vy + vh
        style = self.user.GetWindowLongPtrW(handle, -20)
        app_flags = 0x00040000 | 0x00000080  # WS_EX_APPWINDOW / WS_EX_TOOLWINDOW
        if not visible:
            if handle not in self.hidden_placements:
                self.hidden_placements[handle] = (
                    (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top) if on_screen
                    else (vx + 40, vy + 40, min(1200, vw - 80), min(850, vh - 80)),
                    style & app_flags,
                )
            width, height = max(1, rect.right - rect.left), max(1, rect.bottom - rect.top)
            bounds = (vx - width - 100, vy - height - 100, width, height)
            selected = (style & ~0x00040000) | 0x00000080
            flags = 0x0034 | 0x0080  # NOZORDER | NOACTIVATE | FRAMECHANGED | HIDEWINDOW
        else:
            saved = self.hidden_placements.pop(handle, None)
            if saved is None and on_screen:
                return
            bounds, original = saved or ((vx + 40, vy + 40, min(1200, vw - 80), min(850, vh - 80)), 0x00040000)
            # A monitor may have disappeared while this browser was hidden.
            x, y, width, height = bounds
            if x + width <= vx or x >= vx + vw or y + height <= vy or y >= vy + vh:
                bounds = (vx + 40, vy + 40, min(width, vw - 80), min(height, vh - 80))
            selected = (style & ~app_flags) | original
            flags = 0x0034  # Restore without showing or activating yet.
        if selected != style:
            ctypes.set_last_error(0)
            if not self.user.SetWindowLongPtrW(handle, -20, selected) and ctypes.get_last_error():
                raise ValueError("无法调整店铺窗口后台样式")
        if not self.user.SetWindowPos(handle, 0, *bounds, flags):
            raise ValueError("无法调整店铺窗口后台位置")


def locate(port, profile):
    # Only cold/restarted sessions need process discovery. Command lines stay in
    # memory, are never logged, and both port and the exact profile must match.
    command = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | Where-Object { $_.CommandLine -match '--remote-debugging-port=' -and $_.CommandLine -notmatch '(?:^|\\s)--type=' } | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        encoding="utf-8-sig",
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise ValueError("无法读取店铺浏览器进程")
    processes = json.loads(result.stdout.strip() or "[]")
    if isinstance(processes, dict):
        processes = [processes]
    matches = [p["ProcessId"] for p in processes if matches_process(p.get("CommandLine") or "", port, profile)]
    if len(matches) != 1:
        raise ValueError("无法唯一定位该店铺的 Chrome 进程，未操作其他窗口")
    return matches[0]


class WindowController:
    def __init__(self, port, profile, native=None, discover=locate):
        self.port, self.profile = port, str(profile)
        self.native = native or NativeWindows()
        self.discover = discover
        self.cached = None
        self.lock = threading.RLock()

    def invalidate(self):
        with self.lock:
            self.cached = None

    def process_identity(self, process=None):
        if self.cached and self.native.identity(self.cached[0]) == self.cached:
            return self.cached
        self.cached = None
        # Our own Popen retains the exact command and a live process handle.
        if process is not None and process.poll() is None and isinstance(process.args, (list, tuple)):
            args = subprocess.list2cmdline([str(value) for value in process.args])
            pid = process.pid if matches_process(args, self.port, self.profile) else None
        else:
            pid = None
        pid = pid or self.discover(self.port, self.profile)
        identity = self.native.identity(pid)
        if not identity:
            raise ValueError("该店铺的 Chrome 进程已退出")
        self.cached = identity
        return identity

    def apply(self, visible=None, process=None, activate=True):
        """None is read-only. Always inspect OS state; a cached boolean is unsafe."""
        with self.lock:
            identity = self.process_identity(process)
            windows = self.native.windows(identity[0])
            if not windows:
                raise ValueError("该店铺没有可控制的 Chrome 窗口")
            protect = getattr(self.native, "protect", None)
            if callable(protect) and visible is not None:
                for window in windows:
                    if self.native.identity(identity[0]) != identity:
                        raise ValueError("店铺浏览器已重启，请重试窗口操作")
                    protect(window["handle"], visible is True and activate)
            placement = getattr(self.native, "placement", None)
            if callable(placement) and visible is not None:
                for window in windows:
                    if self.native.identity(identity[0]) != identity:
                        raise ValueError("店铺浏览器已重启，请重试窗口操作")
                    placement(window["handle"], visible)
                # Concealment may already have hidden a window atomically.
                windows = self.native.windows(identity[0])
            changed = (
                []
                if visible is None
                else [w for w in windows if w["visible"] != visible or (visible and (w["minimized"] or activate))]
            )
            for window in changed:
                # Recheck before mutation, including after another window closed.
                if self.native.identity(identity[0]) != identity:
                    self.cached = None
                    raise ValueError("店铺浏览器已重启，请重试窗口操作")
                if activate:
                    self.native.change(window["handle"], visible)
                else:
                    self.native.change(window["handle"], visible, False)
            if changed:
                until = time.monotonic() + 1.5
                while True:
                    if self.native.identity(identity[0]) != identity:
                        self.cached = None
                        raise ValueError("店铺浏览器已重启，请重试窗口操作")
                    windows = self.native.windows(identity[0])
                    if windows and all(
                        w["visible"] == visible and (not visible or not w["minimized"]) for w in windows
                    ):
                        break
                    if time.monotonic() >= until:
                        raise ValueError("窗口显示状态校验失败，请重试")
                    time.sleep(0.03)
            return {"visible": any(w["visible"] for w in windows), "windowCount": len(windows), "changed": len(changed)}

    def guard(self, process=None):
        """Protect newly created work windows without altering visibility."""
        with self.lock:
            identity = self.process_identity(process)
            for window in self.native.windows(identity[0]):
                if self.native.identity(identity[0]) != identity:
                    raise ValueError("店铺浏览器已重启，未操作其他窗口")
                self.native.protect(window["handle"], False)
