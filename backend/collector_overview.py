"""Read-only collector health cards. Polling never runs on Tk's UI thread."""

import json
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
import httpx

METRICS = (
    ("running", "正在采集", "#2563eb"),
    ("queued", "排队任务", "#475569"),
    ("errors", "异常模块", "#c62828"),
    ("waiting_login", "待登录任务", "#a16207"),
    ("deferred", "待调度任务", "#64748b"),
    ("cached", "本机已入库", "#15803d"),
)


def normalize(payload):
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("采集状态尚未就绪")
    values = {key: value if type(value := summary.get(key)) is int and value >= 0 else None for key, _, _ in METRICS}
    config = payload.get("collection", {})
    if not isinstance(config, dict):
        config = {}
    return values, {
        **config,
        "shops": payload.get("shops", {}),
        "deferred_categories": payload.get("deferred_categories", {}),
        "database_write": payload.get("database_write", {}),
        "log_ingestion": payload.get("log_ingestion", {}),
        "delivery": payload.get("delivery", {}),
    }


def read_snapshot(directory, port):
    token = (Path(directory) / "api.key").read_text(encoding="utf-8").strip()
    response = httpx.get(
        f"http://127.0.0.1:{port}/api/system/summary",
        headers={"Authorization": "Bearer " + token},
        timeout=4,
        trust_env=False,
    )
    response.raise_for_status()
    values, config = normalize(response.json())
    update = {}
    try:
        update = json.loads((Path(directory) / "updates/status.json").read_text(encoding="utf-8"))
    except OSError, ValueError:
        pass
    return values, config, update, time.time()


class OverviewPanel:
    def __init__(self, parent, directory):
        import tkinter as tk
        from tkinter import ttk

        self.directory = directory
        self.queue = queue.Queue(maxsize=1)
        self.loading = False
        self.next_poll = 0
        self.selected = None
        self.task_page = 1
        self.task_pages = 1
        self.task_version = 0
        self.task_queue = queue.Queue()
        self.task_loading = False
        self.task_next = 0
        self.port = None
        self.last_success = None
        self.values = {}
        self.config = {}
        self.error = ""
        self.generation = 0
        self.was_running = False
        self.frame = ttk.LabelFrame(parent, text="采集运行概况", padding=10)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(5, weight=1)
        head = ttk.Frame(self.frame)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.concurrency_button = ttk.Button(head, text="并发设置", command=self.open_concurrency, state="disabled")
        self.concurrency_button.pack(side="right", padx=(10, 0))
        self.concurrency_dialog = None
        self.hint = tk.StringVar(value="服务启动后每 5 秒更新")
        ttk.Label(head, textvariable=self.hint).pack(side="right")
        cards = ttk.Frame(self.frame)
        cards.grid(row=1, column=0, sticky="ew")
        for column, (key, label, color) in enumerate(METRICS):
            cards.columnconfigure(column, weight=1, uniform="metric")
            box = tk.Frame(cards, bg="#f1f5f9", padx=10, pady=8)
            box.grid(row=0, column=column, sticky="nsew", padx=(0, 6) if column < 5 else 0)
            tk.Label(box, text=label, bg="#f1f5f9", fg="#526174", font=("Microsoft YaHei UI", 9)).pack(anchor="w")
            value = tk.Label(box, text="—", bg="#f1f5f9", fg=color, font=("Microsoft YaHei UI", 22, "bold"))
            value.pack(anchor="w")
            self.values[key] = value
            for widget in (box, *box.winfo_children()):
                widget.configure(cursor="hand2")
                widget.bind("<Button-1>", lambda event, k=key: self.select(k))
        self.detail = tk.StringVar(value="服务未启动")
        ttk.Label(self.frame, textvariable=self.detail, wraplength=880).grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.update = tk.StringVar()
        ttk.Label(self.frame, textvariable=self.update, wraplength=880, foreground="#a16207").grid(
            row=3, column=0, sticky="w"
        )
        self.logs = tk.StringVar()
        ttk.Label(self.frame, textvariable=self.logs, wraplength=880, foreground="#a16207").grid(
            row=4, column=0, sticky="w"
        )
        self.tasks = ttk.Frame(self.frame, padding=(0, 8))
        self.tasks.columnconfigure(0, weight=1)
        self.tasks.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(self.tasks)
        toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Button(toolbar, text="收起列表", command=self.hide_tasks).pack(side="right")
        self.task_title = tk.StringVar(value="点击上方数字查看任务")
        ttk.Label(toolbar, textvariable=self.task_title, wraplength=680).pack(side="left")
        self.next_button = ttk.Button(toolbar, text="下一页", command=lambda: self.change_page(1))
        self.next_button.pack(side="right")
        self.prev_button = ttk.Button(toolbar, text="上一页", command=lambda: self.change_page(-1))
        self.prev_button.pack(side="right")
        self.tree = ttk.Treeview(
            self.tasks,
            columns=(
                "shop",
                "module",
                "frequency",
                "generated",
                "started",
                "elapsed",
                "remaining",
                "next_run",
                "scope",
                "detail",
            ),
            show="headings",
            height=3,
        )
        for key, label, width in [
            ("shop", "店铺", 230),
            ("module", "任务分类", 180),
            ("frequency", "采集频率", 110),
            ("started", "开始时间", 125),
            ("elapsed", "已运行", 80),
            ("remaining", "超时剩余", 100),
            ("next_run", "下次执行", 125),
            ("generated", "任务生成时间", 125),
            ("scope", "查询范围", 160),
            ("detail", "进度 / 原因", 280),
        ]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=70)
        self.tree.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        scroll = ttk.Scrollbar(self.tasks, orient="vertical", command=self.tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        horizontal = ttk.Scrollbar(self.tasks, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=2, column=0, sticky="ew")
        self.tree.configure(xscrollcommand=horizontal.set)
        self.tree.tag_configure("even", background="#f1f5f9")
        self.tree.bind("<Double-1>", self.show_task_detail)
        self.tree.bind("<Return>", self.show_task_detail)
        self.on_layout_change = lambda: None
        self.tasks.rowconfigure(1, minsize=int(ttk.Style().lookup("Treeview", "rowheight") or 27) * 4 + 6)

    def show_task_detail(self, event=None):
        selected = self.tree.selection()
        if selected:
            from tkinter import messagebox

            messagebox.showinfo(
                "任务详情", "\n".join(str(v) for v in self.tree.item(selected[0], "values")), parent=self.frame
            )

    def open_concurrency(self):
        if not self.was_running or not self.port:
            return
        if self.concurrency_dialog and self.concurrency_dialog.window.winfo_exists():
            self.concurrency_dialog.window.lift()
            return
        from .collector_concurrency import ConcurrencyDialog

        def saved(config):
            self.config.update(config)
            self.next_poll = 0

        self.concurrency_dialog = ConcurrencyDialog(self.frame, self.directory, self.port, saved)

    def hide_tasks(self):
        self.selected = None
        self.task_version += 1
        self.tasks.grid_remove()
        for widget in getattr(self, "compact_hidden", []):
            widget.grid()
        self.on_layout_change()
        for value in self.values.values():
            value.master.configure(highlightbackground="#f1f5f9")

    def select(self, key):
        if self.selected == key:
            self.hide_tasks()
            return
        self.rendered_tasks = None
        self.selected = key
        self.task_page = 1
        self.task_version += 1
        self.task_next = 0
        self.tree.delete(*self.tree.get_children())
        self.task_title.set("正在读取任务…")
        self.tasks.grid(row=5, column=0, sticky="nsew")
        if not getattr(self, "compact_hidden", None):
            self.compact_hidden = [w for w in self.frame.grid_slaves() if int(w.grid_info()["row"]) in (2, 3, 4)]
        for widget in self.compact_hidden:
            widget.grid_remove()
        self.on_layout_change()
        for name, value in self.values.items():
            value.master.configure(highlightthickness=1, highlightbackground="#007aff" if name == key else "#f1f5f9")
        self.task_tick(self.was_running, self.port)

    def change_page(self, delta):
        self.task_page = max(1, min(self.task_pages, self.task_page + delta))
        self.task_version += 1
        self.task_next = 0
        self.task_tick(self.was_running, self.port)

    def task_tick(self, running, port):
        while not self.task_queue.empty():
            version, result, error = self.task_queue.get_nowait()
            self.task_loading = False
            if version != self.task_version:
                continue
            if error:
                self.task_title.set("任务列表暂不可用，稍后自动重试")
                continue
            self.task_pages = result["page_count"]
            self.task_page = result["page"]
            label = next(label for key, label, _ in METRICS if key == self.selected)
            self.task_title.set(f"{label} · {result['total']} 项 · {self.task_page} / {self.task_pages} 页")
            if result.get("categories"):
                self.task_title.set(" · ".join(f"{name} {count}" for name, count in result["categories"].items()))
            self.prev_button.configure(state="normal" if self.task_page > 1 else "disabled")
            self.next_button.configure(state="normal" if self.task_page < self.task_pages else "disabled")
            values = [
                tuple(
                    row.get(k, "")
                    for k in (
                        "shop",
                        "module",
                        "frequency",
                        "generated",
                        "started",
                        "elapsed",
                        "remaining",
                        "next_run",
                        "scope",
                        "detail",
                    )
                )
                for row in result["rows"]
            ]
            if values != getattr(self, "rendered_tasks", None):
                self.tree.delete(*self.tree.get_children())
                for i, row in enumerate(values):
                    self.tree.insert("", "end", values=row, tags=("even",) if i % 2 == 0 else ())
                self.rendered_tasks = values
        if not running or not self.selected or self.task_loading or time.monotonic() < self.task_next:
            return
        self.task_loading = True
        self.task_next = time.monotonic() + 5
        version = self.task_version
        group = self.selected
        page = self.task_page

        def fetch():
            try:
                token = (Path(self.directory) / "api.key").read_text(encoding="utf-8").strip()
                response = httpx.get(
                    f"http://127.0.0.1:{port}/api/system/summary",
                    params={"task_group": group, "page": page},
                    headers={"Authorization": "Bearer " + token},
                    timeout=4,
                    trust_env=False,
                )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result.get("rows"), list):
                    raise ValueError("节点尚未升级")
                self.task_queue.put((version, result, ""))
            except Exception:
                self.task_queue.put((version, None, "unavailable"))

        threading.Thread(target=fetch, daemon=True).start()

    def tick(self, running, port, restarts):
        self.port = port
        self.concurrency_button.configure(state="normal" if running else "disabled")
        if running != self.was_running:
            self.task_version += 1
            self.task_next = 0
            self.rendered_tasks = None
            self.tree.delete(*self.tree.get_children())
            self.generation += 1
            self.was_running = running
            self.next_poll = 0
            self.last_success = None
            for value in self.values.values():
                value.configure(text="—")
        while not self.queue.empty():
            generation, result, error = self.queue.get_nowait()
            self.loading = False
            if generation != self.generation:
                continue
            self.error = error
            if result:
                values, self.config, update, self.last_success = result
                for key, value in values.items():
                    self.values[key].configure(text="—" if value is None else str(value))
                self.update.set(update.get("message", ""))
                self.logs.set(
                    " · ".join(
                        v
                        for v in (
                            write_hint(self.config.get("database_write", {})),
                            delivery_hint(self.config.get("delivery", {})),
                            log_hint(self.config.get("log_ingestion", {})),
                        )
                        if v
                    )
                )
        self.task_tick(running, port)
        if not running:
            self.hint.set("服务未运行")
            self.detail.set(f"服务重启 {restarts} 次")
            self.update.set("")
            return
        stamp = datetime.fromtimestamp(self.last_success).strftime("%H:%M:%S") if self.last_success else "尚未读取"
        age = max(0, int(time.time() - self.last_success)) if self.last_success else None
        self.hint.set(
            (f"数据暂不可用 · 上次 {stamp}" if self.error else f"更新于 {stamp}")
            + (f" · {age} 秒前" if age is not None else "")
        )
        enabled = self.config.get("enabled")
        concurrency = self.config.get("max_concurrent", "—")
        mode = "定时采集已开启" if enabled is True else "定时采集已暂停" if enabled is False else "等待采集状态"
        if self.config.get("home_paused"):
            mode = "首页采集已暂停"
        mode += f" · 同时采集最多 {self.config.get('max_active_shops', 2)} 家店铺"
        categories = " · ".join(f"{name} {count}" for name, count in self.config.get("deferred_categories", {}).items())
        self.detail.set(
            f"{mode} · 最大并发 {concurrency} · 服务重启 {restarts} 次"
            + (" · 当前显示上次成功读取值" if self.error and self.last_success else "")
            + ("\n" + categories if categories else "")
        )
        if self.loading or time.monotonic() < self.next_poll:
            return
        self.loading = True
        self.next_poll = time.monotonic() + 5
        generation = self.generation

        def fetch():
            try:
                self.queue.put((generation, read_snapshot(self.directory, port), ""))
            except Exception:
                self.queue.put((generation, None, "采集状态读取失败"))

        threading.Thread(target=fetch, daemon=True).start()


def log_hint(value, now=None):
    if not value:
        return ""
    parts = ["日志仅保存文件 · 最近 7 天"]
    if value.get("error"):
        parts.append(value["error"])
    return " · ".join(parts)


def delivery_hint(value):
    lanes = [v for v in value.values() if v]
    if not lanes:
        return "中心回传状态待确认（本机已入库不代表中心已接收）"
    pending = sum(v.get("pending", 0) for v in lanes)
    failed = sum(v.get("failed", 0) for v in lanes)
    parts = [f"待回传中心 {pending} 份", f"回传异常 {failed} 份"]
    rejected = sum(v.get("rejected_count", 0) for v in lanes)
    if rejected:
        parts.append(f"中心拒收 {rejected} 份（保留本地，数据更新后重试）")
    stamp = max((v.get("last_success", 0) for v in lanes), default=0)
    if stamp:
        parts.append("最近回传 " + datetime.fromtimestamp(stamp).strftime("%H:%M:%S"))
    errors = list(dict.fromkeys(v["error"] for v in lanes if v.get("error")))
    errors.extend(dict.fromkeys(v["rejected_error"] for v in lanes if v.get("rejected_error")))
    return " · ".join(parts + errors)


def write_hint(value):
    if not value:
        return "数据库写入状态待确认"
    state = "写入拥堵，暂缓新采集" if value.get("pressure") else "数据库写入正常"
    if value.get("sharded"):
        state = f"独立店铺库 {value.get('shop_count', 0)} 个 · 拥堵店铺 {value.get('pressured_shops', 0)} 家"
    current = (
        (" · 当前 " + value.get("current", "") + f" {value.get('active_seconds', 0)}秒") if value.get("active") else ""
    )
    return (
        f"{state} · 待写 {value.get('pending', 0)} 批 · 最长等待 {value.get('oldest_seconds', 0)}秒 · 最近写入 {value.get('last_ms', 0)}毫秒 · 累计超时 {value.get('timeouts', 0)}次"
        + current
        + (" · 等待其他进程释放写入" if value.get("external_wait") else "")
    )
