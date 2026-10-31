"""Local concurrency editor; only changed fields are sent to the collector API."""

import queue
import threading
from pathlib import Path
import httpx

FIELDS = {"max_concurrent": "节点总并发", "shop_concurrent": "默认单店并发"}


def changes(values, original):
    parsed = {}
    for key, label in FIELDS.items():
        try:
            value = int(str(values[key]).strip())
        except ValueError, TypeError:
            raise ValueError(label + "应为正整数") from None
        if value < 1:
            raise ValueError(label + "应为正整数")
        parsed[key] = value
    return {k: v for k, v in parsed.items() if v != original.get(k)}


def request(directory, port, patch=None):
    token = (Path(directory) / "api.key").read_text(encoding="utf-8").strip()
    with httpx.Client(
        base_url=f"http://127.0.0.1:{port}", headers={"Authorization": "Bearer " + token}, timeout=15, trust_env=False
    ) as client:
        if patch:
            response = client.put("/api/system/concurrency", json=patch)
            if response.status_code == 400:
                detail = response.json().get("detail")
                raise ValueError(detail if isinstance(detail, str) else "并发设置无效")
            response.raise_for_status()
        response = client.get("/api/system/settings")
        response.raise_for_status()
        config = response.json()["collection"]
        if patch and any(config.get(k) != v for k, v in patch.items()):
            raise ValueError("读取到的配置与提交值不同，请重新读取后确认")
        return config


class ConcurrencyDialog:
    def __init__(self, parent, directory, port, on_saved):
        import tkinter as tk
        from tkinter import ttk

        self.directory = directory
        self.port = port
        self.on_saved = on_saved
        self.results = queue.Queue()
        self.original = None
        self.busy = False
        self.window = tk.Toplevel(parent)
        self.window.title("本机采集并发设置")
        self.window.resizable(False, False)
        self.window.transient(parent.winfo_toplevel())
        body = ttk.Frame(self.window, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="仅修改当前采集节点", font=("Microsoft YaHei UI", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 14)
        )
        self.values = {}
        self.entries = []
        for row, (key, label) in enumerate(FIELDS.items(), 1):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=(0, 24), pady=6)
            value = tk.StringVar()
            self.values[key] = value
            entry = ttk.Entry(body, textvariable=value, width=16, state="disabled")
            entry.grid(row=row, column=1, pady=6)
            self.entries.append(entry)
        ttk.Label(
            body,
            text="只限制节点总并发和单店并发；同店同业务模块串行。\n保存后自动应用，已运行任务正常完成。",
            foreground="#64748b",
        ).grid(row=7, column=0, columnspan=2, sticky="w")
        self.message = tk.StringVar(value="正在读取本机配置…")
        ttk.Label(body, textvariable=self.message, wraplength=390).grid(
            row=8, column=0, columnspan=2, sticky="w", pady=12
        )
        actions = ttk.Frame(body)
        actions.grid(row=9, column=0, columnspan=2, sticky="e")
        self.reload = ttk.Button(actions, text="重新读取", command=lambda: self.start(None))
        self.reload.pack(side="left", padx=6)
        self.save = ttk.Button(actions, text="保存本机设置", command=self.submit, state="disabled")
        self.save.pack(side="left")
        self.start(None)
        self.window.after(100, self.poll)

    def start(self, patch):
        if self.busy:
            return
        self.busy = True
        self.save.configure(state="disabled")
        self.reload.configure(state="disabled")
        for entry in self.entries:
            entry.configure(state="disabled")
        self.message.set("正在保存并核实…" if patch else "正在读取本机配置…")

        def work():
            try:
                self.results.put((request(self.directory, self.port, patch), None, bool(patch)))
            except ValueError as error:
                self.results.put((None, str(error), False))
            except Exception:
                self.results.put((None, "连接失败或请求超时；请重新读取确认保存结果。", False))

        threading.Thread(target=work, daemon=True).start()

    def submit(self):
        if self.busy or self.original is None:
            return
        try:
            patch = changes({k: v.get() for k, v in self.values.items()}, self.original)
        except ValueError as error:
            self.message.set(str(error))
            return
        if not patch:
            self.message.set("没有修改项")
            return
        self.start(patch)

    def poll(self):
        if not self.window.winfo_exists():
            return
        try:
            config, error, saved = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.reload.configure(state="normal")
            if config:
                self.original = config
                for key, value in self.values.items():
                    value.set(config[key])
                self.message.set(
                    "已保存，并确认本机读取值一致。中心将随节点状态更新。" if saved else "已读取当前本机配置"
                )
                if saved:
                    self.on_saved(config)
            else:
                self.message.set(error)
            if self.original is not None:
                self.save.configure(state="normal")
                for entry in self.entries:
                    entry.configure(state="normal")
        self.window.after(100, self.poll)
