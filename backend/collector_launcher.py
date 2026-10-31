"""Portable Windows collector control panel. No admin or installed Python needed."""

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time
import httpx
from .center.address import center_url
from .collector_runtime import executable
from .dpapi import protect

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".data"
CONFIG = DATA / "collector-connection.local.json"


def identify(url, token):
    if len(token) < 32:
        raise ValueError("请填写添加采集节点时生成的节点密钥，不是管理员登录密钥")
    with httpx.Client(timeout=12, trust_env=False, follow_redirects=False) as client:
        try:
            response = client.get(url + "/agent/identify", headers={"Authorization": "Bearer " + token})
        except httpx.ConnectError:
            raise ValueError("无法连接中心，请检查中心 IP、8010 监听、防火墙和两台电脑的网络连通性") from None
        except httpx.TimeoutException:
            raise ValueError("连接中心超时，请检查网络或防火墙是否允许采集电脑访问中心端口") from None
    if response.status_code == 401:
        raise ValueError("节点密钥无效或已删除，请在中心重新添加节点")
    if response.status_code in (404, 307, 308):
        raise ValueError("中心入口未接入采集服务，请检查 /agent/* 反向代理；本机使用 8010 端口")
    response.raise_for_status()
    value = response.json()
    if not value.get("id") or not value.get("name"):
        raise ValueError("返回内容不是中心节点信息")
    return value


def load_config():
    value = json.loads(CONFIG.read_text(encoding="utf-8"))
    value["token"] = protect(value.pop("protected_token"), True)
    return value


def save_config(value):
    DATA.mkdir(exist_ok=True)
    saved = {k: v for k, v in value.items() if k != "token"}
    saved["protected_token"] = protect(value["token"])
    temp = CONFIG.with_suffix(".tmp")
    temp.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    temp.replace(CONFIG)


def startup(enabled):
    import winreg

    command = f'"{executable(ROOT, True)}" "{ROOT / "collector-launcher.py"}" --auto'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        if enabled:
            winreg.SetValueEx(key, "ECMPCollector", 0, winreg.REG_SZ, command)
        else:
            try:
                current = winreg.QueryValueEx(key, "ECMPCollector")[0]
                if current == command:
                    winreg.DeleteValue(key, "ECMPCollector")
            except FileNotFoundError:
                pass


class Runner:
    def __init__(self, root=ROOT, port=8001):
        self.ui_handoff = True
        self.restart_requested = False
        self.root = root
        self.port = port
        self.children = {}
        self.logs = {}
        self.config = None
        self.restart_after = {}
        self.restarts = 0
        self.running = False

    def occupied(self):
        with socket.socket() as sock:
            return sock.connect_ex(("127.0.0.1", self.port)) == 0

    def spawn(self, kind):
        env = os.environ.copy()
        for name in (
            "ECMP_DATABASE_URL",
            "ECMP_CENTER_ADMIN_TOKEN",
            "PDD_API_TOKEN",
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
        ):
            env.pop(name, None)
        env.update(
            PDD_DATA_DIR=str(self.root / ".data"),
            ECMP_CENTER_URL=self.config["url"],
            ECMP_NODE_ID=self.config["id"],
            ECMP_NODE_TOKEN=self.config["token"],
            ECMP_LOCAL_PORT=str(self.port),
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
        )
        env["PYTHONUNBUFFERED"] = "1"
        # Only a launcher with data rollback support may activate this format.
        env["ECMP_SHOP_DATABASES"] = "1"
        if kind == "collector" and (self.root / ".data/updates/journal.json").exists():
            env["ECMP_UPDATE_WARMUP_SECONDS"] = "300"
        browser = self.root / ".browsers/chrome-win64/chrome.exe"
        if browser.exists():
            env["PDD_CHROME_PATH"] = str(browser)
        args = (
            ["-m", "uvicorn", "backend.collector_server:app", "--host", "127.0.0.1", "--port", str(self.port)]
            if kind == "collector"
            else ["-m", "backend.center.agent"]
        )
        logdir = self.root / ".data/launcher-logs"
        logdir.mkdir(parents=True, exist_ok=True)
        if kind in self.logs:
            self.logs[kind].close()
        self.children[kind] = subprocess.Popen(
            [str(executable(self.root)), *args],
            cwd=self.root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        from .collector_logs import ProcessLog

        self.logs[kind] = ProcessLog(self.children[kind].stdout, logdir, kind, [self.config.get("token", "")])

    def start(self, config):
        if self.running:
            raise ValueError("采集服务已启动，请先停止再修改连接")
        if self.occupied():
            raise ValueError(f"本机 {self.port} 端口已被占用，请先停止原采集服务，避免重复启动")
        self.config = config
        try:
            self.spawn("collector")
            deadline = time.monotonic() + 180
            startup_limit = time.monotonic() + 3600
            while time.monotonic() < deadline:
                if self.children["collector"].poll() is not None:
                    raise ValueError("采集服务启动失败，请查看采集服务当日日志")
                try:
                    response = httpx.get(f"http://127.0.0.1:{self.port}/health", timeout=1, trust_env=False)
                    if response.is_success:
                        break
                except httpx.HTTPError:
                    pass
                from .collector_update import read_json

                migration = read_json(self.root / ".data/storage-migration-status.json", {})
                if migration.get("state") == "migrating" and time.time() - migration.get("updated", 0) < 120:
                    deadline = min(startup_limit, time.monotonic() + 180)
                time.sleep(0.4)
            else:
                raise ValueError("采集服务启动超时，请查看日志")
            self.spawn("agent")
            for _ in range(15):
                time.sleep(0.2)
                if self.children["agent"].poll() is not None:
                    raise ValueError("回传服务启动失败，请查看中心连接当日日志")
            self.running = True
            from .collector_update import resume_probation, read_json

            resume_probation(self)
            # A legacy UI-only handoff already completed probation before the
            # restart. Clear only that stale message after both children start.
            saved = read_json(self.root / ".data/updates/status.json", {})
            if (
                saved.get("message") == "界面更新完成，正在自动重启"
                and not (self.root / ".data/updates/journal.json").exists()
            ):
                from .collector_update import write_json

                write_json(
                    self.root / ".data/updates/status.json",
                    {**saved, "message": "自动升级成功", "restarted_at": time.time()},
                )
        except Exception:
            self.stop()
            raise

    def monitor(self):
        if not self.running:
            return
        for kind in ("collector", "agent"):
            process = self.children.get(kind)
            if process is not None and process.poll() is None:
                continue
            due = self.restart_after.setdefault(kind, time.monotonic() + 10)
            if time.monotonic() < due:
                continue
            if kind == "collector" and self.occupied():
                raise ValueError("采集服务端口被其他程序占用，已停止自动重启，请检查日志")
            self.spawn(kind)
            self.restarts += 1
            self.restart_after.pop(kind, None)

    def stop(self):
        self.running = False
        for kind in ("agent", "collector"):
            child = self.children.get(kind)
            if child and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
        self.children.clear()
        self.restart_after.clear()
        for log in self.logs.values():
            log.close()
        self.logs.clear()


def console_panels(parent, directory, secrets):
    import tkinter as tk
    from tkinter import ttk
    from .collector_overview import OverviewPanel
    from .collector_logs import LogPanel

    panes = ttk.Frame(parent)
    panes.columnconfigure(0, weight=1)
    panes.rowconfigure(1, weight=1)
    # Retain the lightweight status reader for title counts and configuration,
    # without showing task cards or a task table in the console.
    overview = OverviewPanel(panes, directory)
    bar = ttk.Frame(panes)
    bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
    ttk.Button(bar, text="采集参数", command=overview.open_concurrency).pack(side="right")
    ttk.Label(bar, text="任务浏览器可见 · 日志自动更新").pack(side="left")
    notebook = ttk.Notebook(panes)
    notebook.grid(row=1, column=0, sticky="nsew")
    operations = LogPanel(notebook, directory, secrets, mode="operations")
    panel = LogPanel(notebook, directory, secrets)
    notebook.add(operations.frame, text="操作日志")
    notebook.add(panel.frame, text="运行日志")
    panel.operations = operations
    panel.notebook = notebook
    return panes, overview, panel


def main():
    import tkinter as tk
    from tkinter import ttk, messagebox

    if os.name != "nt":
        raise RuntimeError("此启动器用于 Windows x64")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    mutex = kernel.CreateMutexW(
        None, False, "Local\\ECMPCollector-" + hashlib.sha256(str(ROOT).lower().encode()).hexdigest()
    )
    if ctypes.get_last_error() == 183:
        messagebox.showinfo("采集服务", "此目录的控制面板已经打开，请从任务栏恢复。")
        return
    from .collector_update import startup as recover_update, update, read_json, probation, recover, retry_delay

    try:
        recover_update(ROOT)
    except Exception:
        messagebox.showerror("升级恢复失败", "上次升级未完成，恢复失败。请保留 .data/updates 并联系管理员。")
        return
    from .collector_restart import fingerprint, needed, prepare, launch

    loaded_ui = fingerprint(ROOT)
    restart_ui = False
    restart_minimized = False
    restart_check = 0
    restart_delay = [60]
    window = tk.Tk()
    window.title("多店铺数据工作台 · 采集服务")
    height = min(900, max(560, window.winfo_screenheight() - 100))
    window.geometry(f"1000x{height}")
    window.minsize(850, 560)
    style = ttk.Style()
    style.theme_use("clam")
    style.configure(".", font=("Microsoft YaHei UI", 10), background="#f2f2f7")
    style.configure("TButton", padding=(10, 6))
    style.configure("Treeview", rowheight=27, background="#ffffff", fieldbackground="#ffffff", borderwidth=0)
    style.configure("Treeview.Heading", background="#e5e5ea", padding=6)
    style.configure("TEntry", padding=6)
    style.configure("TLabel", foreground="#20334c")
    style.configure("Status.TLabel", background="#e8eef8", foreground="#24456b", padding=10)
    frame = ttk.Frame(window, padding=18)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)
    heading = ttk.Frame(frame)
    heading.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
    title = tk.StringVar(value="采集服务 · 已登录 — 家")
    ttk.Label(heading, textvariable=title, font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
    connection = ttk.LabelFrame(frame, text="连接设置", padding=12)
    connection.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
    connection.columnconfigure(1, weight=1)
    url = tk.StringVar()
    token = tk.StringVar()
    auto = tk.BooleanVar()
    status = tk.StringVar(value="填写中心地址后申请接入，中心批准后自动启动")
    node = tk.StringVar(value="节点：尚未识别")
    ttk.Label(connection, text="中心地址").grid(row=1, column=0, sticky="w", padx=(0, 12))
    url_entry = ttk.Entry(connection, textvariable=url)
    url_entry.grid(row=1, column=1, sticky="ew", pady=4)
    ttk.Label(connection, text="密钥自动生成并加密保存 · 首次接入需中心审批").grid(
        row=2, column=0, columnspan=2, sticky="w", pady=4
    )
    ttk.Label(connection, textvariable=node).grid(row=3, column=0, columnspan=2, sticky="w", pady=6)
    auto_box = ttk.Checkbutton(connection, text="登录 Windows 后自动启动（当前用户）", variable=auto)
    auto_box.grid(row=4, column=0, columnspan=2, sticky="w")
    ttk.Label(connection, text="关闭窗口后继续采集 · 每台电脑使用独立节点密钥", wraplength=620).grid(
        row=5, column=0, columnspan=2, sticky="w", pady=4
    )
    expanded = tk.BooleanVar(value=not CONFIG.exists())

    def toggle_connection():
        expanded.set(not expanded.get())
        render_connection()

    def render_connection():
        for widget in connection.winfo_children():
            if int(widget.grid_info().get("row", getattr(widget, "_connection_row", 0))) > 0:
                widget._connection_row = int(
                    widget.grid_info().get("row", widget._connection_row if hasattr(widget, "_connection_row") else 0)
                )
                if expanded.get():
                    widget.grid()
                else:
                    widget.grid_remove()
        connection_toggle.configure(text="收起连接设置" if expanded.get() else "展开连接设置")

    connection_toggle = ttk.Button(connection, text="连接设置", command=toggle_connection)
    connection_toggle.grid(row=0, column=0, sticky="w")
    ttk.Label(connection, textvariable=node).grid(row=0, column=1, sticky="e")
    render_connection()
    ttk.Label(frame, textvariable=status, style="Status.TLabel", wraplength=880).grid(
        row=9, column=0, columnspan=2, sticky="ew", pady=(8, 0)
    )
    buttons = ttk.Frame(frame)
    buttons.grid(row=7, column=0, columnspan=2, sticky="w", pady=8)
    runner = Runner()
    messages = queue.Queue()
    quiet_messages = {}
    busy = False
    last_check = 0
    last_update = 0
    last_trial = 0
    last_recover = 0
    connected_config = None
    center = [None, 0.0]
    manual_stopped = False
    pending_config = None
    last_enrollment = 0
    from .collector_logs import LogPanel
    from .collector_overview import OverviewPanel

    panes, overview, panel = console_panels(frame, DATA, lambda: [token.get(), (runner.config or {}).get("token", "")])
    panes.grid(row=8, column=0, columnspan=2, sticky="nsew")
    frame.rowconfigure(8, weight=1)
    panel.status("控制面板已打开")

    def work(fn, quiet=False):
        nonlocal busy
        if busy:
            return
        busy = True
        start.configure(state="disabled")
        stop.configure(state="disabled")
        if not quiet:
            panel.status("正在执行操作，请稍候")

        def job():
            try:
                messages.put(("quiet" if quiet else "ok", fn()))
            except Exception as error:
                reason = str(error) if isinstance(error, ValueError) else "连接或启动失败，请检查中心地址、网络和日志。"
                messages.put(("error", reason))

        threading.Thread(target=job, daemon=True).start()

    def start_clicked(quiet=False):
        nonlocal connected_config, manual_stopped, pending_config, last_enrollment
        manual_stopped = False
        entered_url = url.get()
        autostart = auto.get()

        def task():
            nonlocal connected_config, pending_config, last_enrollment
            from .collector_enrollment import configuration, enroll, code

            address = center_url(entered_url)
            try:
                current = load_config()
            except FileNotFoundError:
                current = None
            config = configuration(address, current, autostart)
            if config != current:
                save_config(config)
                startup(autostart)
            if config.get("enrollment"):
                pending_config = config
                last_enrollment = time.monotonic()
                value = enroll(config, read_json(ROOT / "collector-version.json", {}).get("version", ""))
                if value["state"] != "approved":
                    if value["state"] != "pending":
                        pending_config = None
                    labels = {
                        "pending": "等待中心确认",
                        "rejected": "中心已拒绝接入，请联系管理员",
                        "revoked": "节点权限已撤销，请联系管理员",
                        "expired": "申请已过期，请联系管理员",
                    }
                    return labels[value["state"]] + " · 机器 " + config["name"] + " · 核对码 " + code(config)
                config.update(id=value["id"], name=value["name"], enrollment=False)
                save_config(config)
                pending_config = None
            else:
                identity = identify(address, config["token"])
                config.update(id=identity["id"], name=identity["name"])
            connected_config = config
            try:
                runner.start(config)
            except Exception:
                upgrade = read_json(ROOT / ".data/updates/status.json", {})
                if upgrade.get("resume_probation"):
                    from .collector_update import revert

                    return revert(
                        runner, config, upgrade.get("version"), "新版控制台接续启动失败，已回退并恢复旧版服务"
                    )
                raise
            save_config(config)
            startup(autostart)
            return "采集服务已启动，正在建立中心连接"

        work(task, quiet=quiet)

    def stop_clicked():
        nonlocal manual_stopped
        manual_stopped = True
        work(lambda: runner.stop() or "采集服务已停止，配置已保留")

    start = ttk.Button(
        heading, text="▶ 启动", command=lambda: stop_clicked() if runner.running or pending_config else start_clicked()
    )
    start.pack(side="right")
    stop = start
    update_button = ttk.Button(buttons, text="检查更新", command=lambda: work(lambda: update(runner, force=True)))
    update_button.pack(side="left", padx=(0, 10))

    def logs():
        folder = DATA / "launcher-logs"
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    ttk.Button(buttons, text="打开日志", command=logs).pack(side="left", padx=(0, 10))

    def quit_app():
        if busy:
            return
        if runner.running and not messagebox.askyesno("退出", "退出控制面板将停止采集服务，是否继续？"):
            return
        runner.stop()
        window.destroy()

    ttk.Button(buttons, text="退出", command=quit_app).pack(side="left")
    window.protocol("WM_DELETE_WINDOW", window.iconify)
    if "--minimized" in sys.argv:
        window.after(100, window.iconify)
    try:
        config = load_config()
        url.set(config["url"])
        token.set(config["token"])
        auto.set(config.get("auto", False))
        node.set("节点：" + config.get("name", ""))
    except FileNotFoundError:
        pass
    except Exception:
        status.set("配置无法解密，请使用原 Windows 用户或联系管理员，不会覆盖原凭据")

    def tick():
        nonlocal \
            busy, \
            last_check, \
            last_update, \
            last_trial, \
            last_recover, \
            restart_ui, \
            restart_minimized, \
            restart_check, \
            last_enrollment
        if runner.restart_requested:
            restart_ui = True
            restart_minimized = window.state() == "iconic"
            window.destroy()
            return
        while not messages.empty():
            kind, text = messages.get()
            if kind == "identity":
                if connected_config and text.get("id") == connected_config.get("id"):
                    connected_config["name"] = text["name"]
                continue
            if kind == "restart":
                restart_ui = True
                restart_minimized = window.state() == "iconic"
                window.destroy()
                return
            if kind != "quiet" or text != quiet_messages.get(kind):
                panel.status(text, "error" if kind == "error" else None)
            if kind == "quiet":
                quiet_messages[kind] = text
            if kind == "status":
                if not busy and runner.running:
                    status.set(text)
            else:
                busy = False
                if text:
                    status.set(text)
        active = runner.running
        for widget in (url_entry, auto_box):
            widget.configure(state="disabled" if active or busy else "normal")
        start.configure(state="disabled" if busy else "normal", text="■ 停止" if active or pending_config else "▶ 启动")
        count = overview.config.get("shops", {}).get("logged_in") if active else 0
        title.set(
            (connected_config or {}).get("name", "采集服务")
            + f" · 已登录 {count if count is not None else chr(8212)} 家店铺"
        )
        sleeping_count = overview.config.get("shops", {}).get("sleeping", 0) if active else 0
        if sleeping_count:
            title.set(title.get() + f" · 休眠 {sleeping_count} 家")
        window.title(title.get() + " · 多店铺数据工作台")
        update_button.configure(state="disabled" if not active or busy else "normal")
        if connected_config:
            node.set(
                "节点："
                + connected_config["name"]
                + " · 版本 "
                + read_json(ROOT / "collector-version.json", {}).get("version", "手动部署")
            )
        if (
            not active
            and not busy
            and pending_config
            and not manual_stopped
            and time.monotonic() - last_enrollment >= 15
        ):
            last_enrollment = time.monotonic()
            start_clicked(quiet=True)
        journal = (ROOT / ".data/updates/journal.json").exists()
        if active and not busy and journal and time.monotonic() - last_trial > 15:
            last_trial = time.monotonic()
            work(lambda: probation(runner, *center), quiet=True)
        if (
            not active
            and not busy
            and not manual_stopped
            and connected_config
            and time.monotonic() - last_recover > 60
            and read_json(ROOT / ".data/updates/status.json", {}).get("stopped")
        ):
            last_recover = time.monotonic()
            work(lambda: recover(runner))
        if active and not busy and time.monotonic() - restart_check > restart_delay[0]:
            restart_check = time.monotonic()
            if needed(ROOT, loaded_ui):

                def restart_task():
                    try:
                        prepare(runner)
                    except Exception:
                        restart_delay[0] = min(1800, restart_delay[0] * 2)
                        raise
                    messages.put(("restart", ""))
                    return "界面更新完成，正在自动重启"

                work(restart_task)
        if active and not busy:
            try:
                runner.monitor()
            except Exception as error:
                runner.stop()
                status.set(str(error))
                panel.status(str(error), "error")
            if time.monotonic() - last_check > 15:
                last_check = time.monotonic()

                def check():
                    try:
                        identity = identify(connected_config["url"], connected_config["token"])
                        center[:] = [bool(identity.get("connected")), time.time()]
                        if identity["name"] != connected_config.get("name"):
                            updated = {**connected_config, "name": identity["name"]}
                            save_config(updated)
                            messages.put(("identity", identity))
                        messages.put(
                            (
                                "status",
                                ("中心已连接" if identity.get("connected") else "正在连接中心")
                                + (f" · 自动恢复 {runner.restarts} 次" if runner.restarts else " · 运行正常"),
                            )
                        )
                    except ValueError as error:
                        center[:] = [None, time.time()]
                        messages.put(("status", str(error) + "；本地采集继续运行"))
                    except Exception:
                        center[:] = [None, time.time()]
                        messages.put(("status", "中心暂不可达，正在自动重连；本地采集继续运行"))

                threading.Thread(target=check, daemon=True).start()
            if (
                not journal
                and time.monotonic() - last_update > 60
                and time.monotonic() - last_update > retry_delay(ROOT)
                and (ROOT / "collector-update-public.pem").exists()
            ):
                last_update = time.monotonic()
                work(lambda: update(runner), quiet=True)
        panel.tick()
        panel.operations.tick()
        overview.tick(runner.running, runner.port, runner.restarts)
        window.after(1000, tick)

    def fast_tasks():
        overview.task_tick(runner.running, runner.port)
        window.after(150, fast_tasks)

    fast_tasks()
    tick()
    if "--configure" not in sys.argv and token.get():
        window.after(200, start_clicked)
    try:
        window.mainloop()
    finally:
        runner.stop()
        kernel.CloseHandle(mutex)
        if restart_ui:
            launch(ROOT, restart_minimized)


if __name__ == "__main__":
    main()
