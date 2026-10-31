"""Batch operations restricted to this service's Chrome profiles and debug ports."""

import ctypes
import json
import math
import os
import re
import subprocess
import time
from ctypes import wintypes


def tile_rectangles(count, area):
    left, top, width, height = area
    if count < 1:
        return []
    if width < 1 or height < 1:
        raise ValueError("屏幕可用区域无效")
    # Balance rows, filling the last row rather than leaving unused grid cells.
    choices = []
    for rows in range(1, count + 1):
        columns = [count // rows + (i < count % rows) for i in range(rows)]
        if max(columns) > width or rows > height:
            continue
        score = sum(n * math.log((width / n) / (height / rows) / 1.6) ** 2 for n in columns) / count
        choices.append((score, rows, columns))
    if not choices:
        raise ValueError("窗口数量超过屏幕可排列范围")
    _, rows, columns = min(choices)
    return [
        (
            left + width * c // n,
            top + height * r // rows,
            width * (c + 1) // n - width * c // n,
            height * (r + 1) // rows - height * r // rows,
        )
        for r, n in enumerate(columns)
        for c in range(n)
    ]


def matches_process(command, port, profile):
    if not re.search(r"(?:^|\s)--remote-debugging-port=" + str(port) + r"(?=\s|$)", command):
        return False
    if re.search(r"(?:^|\s)--type=", command):
        return False
    path = re.escape(os.path.abspath(profile))
    return bool(
        re.search(
            r'(?:^|\s)(?:"--user-data-dir='
            + path
            + r'"|--user-data-dir="'
            + path
            + r'"|--user-data-dir='
            + path
            + r"(?=\s|$))",
            command,
            re.I,
        )
    )


class Rect(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


def native_windows(records, action):
    if os.name != "nt":
        raise ValueError("本批量窗口功能需要 Windows 桌面；无界面服务请使用登录画面")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    signatures = {
        "EnumWindows": ([callback_type, wintypes.LPARAM], wintypes.BOOL),
        "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
        "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
        "ShowWindowAsync": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        "SetWindowPos": (
            [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT],
            wintypes.BOOL,
        ),
        "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(Rect)], wintypes.BOOL),
        "SystemParametersInfoW": ([wintypes.UINT, wintypes.UINT, ctypes.POINTER(Rect), wintypes.UINT], wintypes.BOOL),
        "SetThreadDpiAwarenessContext": ([ctypes.c_void_p], ctypes.c_void_p),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(user32, name)
        fn.argtypes = args
        fn.restype = result
    previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    try:
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
        owners = {}
        errors = {}
        handles = {r["id"]: [] for r in records}
        for record in records:
            matches = [
                p for p in processes if matches_process(p.get("CommandLine") or "", record["port"], record["profile"])
            ]
            if len(matches) != 1:
                errors[record["id"]] = "无法唯一定位该店铺的浏览器进程"
                continue
            owners[matches[0]["ProcessId"]] = record["id"]

        @callback_type
        def collect(handle, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            if pid.value in owners:
                name = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(handle, name, 256)
                if name.value == "Chrome_WidgetWin_1" and user32.GetWindowTextLengthW(handle) > 0:
                    handles[owners[pid.value]].append(handle)
            return True

        user32.EnumWindows(collect, 0)
        windows = [(record["id"], h) for record in records for h in sorted(handles[record["id"]])]
        for record in records:
            if not handles[record["id"]] and record["id"] not in errors:
                errors[record["id"]] = "该店铺没有可控制的窗口"
        area = None
        planned = []
        if action == "tile":
            work = Rect()
            if not user32.SystemParametersInfoW(0x30, 0, ctypes.byref(work), 0):
                raise ValueError("无法读取屏幕可用区域")
            area = (work.left, work.top, work.right - work.left, work.bottom - work.top)
            planned = tile_rectangles(len(windows), area)
        if action not in ("show", "hide", "tile", "query"):
            raise ValueError("窗口操作无效")
        for sid, handle in [] if action == "query" else windows:
            if not user32.ShowWindowAsync(handle, 0 if action == "hide" else 9):
                errors[sid] = "窗口显示状态未能更新"
        deadline = time.monotonic() + 3
        while (
            action != "query"
            and time.monotonic() < deadline
            and any(bool(user32.IsWindowVisible(h)) != (action != "hide") for _, h in windows)
        ):
            time.sleep(0.1)
        if action == "tile":
            for (sid, handle), (x, y, w, h) in zip(windows, planned):
                # Bypass Chrome's minimum drag size. No activation or always-on-top changes.
                if not user32.SetWindowPos(handle, None, x, y, w, h, 0x4 | 0x10 | 0x40 | 0x400 | 0x4000):
                    errors[sid] = "窗口定位失败"
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                complete = True
                for (_, handle), box in zip(windows, planned):
                    rect = Rect()
                    user32.GetWindowRect(handle, ctypes.byref(rect))
                    if (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top) != box:
                        complete = False
                        break
                if complete:
                    break
                time.sleep(0.1)
        for index, (sid, handle) in enumerate(windows):
            if action != "query" and bool(user32.IsWindowVisible(handle)) != (action != "hide"):
                errors[sid] = "窗口显示状态校验失败"
            if action == "tile":
                rect = Rect()
                user32.GetWindowRect(handle, ctypes.byref(rect))
                actual = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
                if actual != planned[index]:
                    errors[sid] = "窗口未能调整到指定区域，请重试平铺"
        return {
            "results": [
                {
                    "shop_id": r["id"],
                    "ok": r["id"] not in errors,
                    "error": errors.get(r["id"], ""),
                    "windows": len(handles[r["id"]]),
                    "visible": any(user32.IsWindowVisible(h) for h in handles[r["id"]]),
                }
                for r in records
            ],
            "window_count": len(windows),
            "work_area": area,
            "layout": planned,
        }
    finally:
        if previous:
            user32.SetThreadDpiAwarenessContext(previous)
