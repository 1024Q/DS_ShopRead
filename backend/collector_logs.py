"""Bounded, read-only log feed for the portable collector panel."""

from collections import deque, OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3


def redact(text, secrets=()):
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[已隐藏]")
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[已隐藏]", text)
    return re.sub(
        r"""(?i)((?:password|passwd|token|cookie|authorization|secret|密码|密钥)["']?\s*[=:：]\s*)("[^"]*"|'[^']*'|[^\s,;}]+)""",
        r"\1[已隐藏]",
        text,
    )


def level_of(text):
    explicit = re.search(r"\b(ERROR|CRITICAL|WARNING|WARN|INFO|DEBUG)\b", text)
    if explicit:
        return {
            "error": "error",
            "critical": "error",
            "warning": "warning",
            "warn": "warning",
            "info": "info",
            "debug": "debug",
        }[explicit[1].lower()]
    text = re.sub(r"异常重启\s*0\s*次", "运行正常", text)
    if re.search(r"error|exception|traceback|critical|失败|异常|错误", text, re.I):
        return "error"
    if re.search(r"warning|warn\b|警告|重试|重连", text, re.I):
        return "warning"
    if re.search(r"success|成功|已启动|已连接|升级完成", text, re.I):
        return "success"
    if re.search(r"debug|调试", text, re.I):
        return "debug"
    return "info"


BEIJING = timezone(timedelta(hours=8))
import threading
import time

_shop_log_lock = threading.Lock()
_shop_log_seen = {}
_shop_writers = OrderedDict()


def shop_log_source(shop_id, name):
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip(" .")[:70].rstrip(" .")
    if not safe:
        raise ValueError('店铺日志必须有店铺名称')
    return f"{safe}-shop-{int(shop_id)}"


def shop_event(folder, shop_id, level, module, event, message, name=""):
    if not shop_id or not str(name).strip(' .') or (level in ("debug", "info") and any(s in message for s in ("开始读取", "已保存分页进度"))):
        return
    key = (str(folder), shop_id, module, event, message)
    with _shop_log_lock:
        now = time.monotonic()
        if now - _shop_log_seen.get(key, -1000) < (60 if level in ("error", "warning") else 300):
            return
        if len(_shop_log_seen) > 4096:
            for old in list(_shop_log_seen):
                if now - _shop_log_seen[old] > 300:
                    del _shop_log_seen[old]
            if len(_shop_log_seen) > 4096:
                _shop_log_seen.clear()
        writer_key = (str(folder), int(shop_id), shop_log_source(shop_id, name))
        if writer_key not in _shop_writers:
            if len(_shop_writers) >= 512:
                _shop_writers.popitem(last=False)
            _shop_writers[writer_key] = DailyLog(folder, writer_key[2])
        _shop_writers.move_to_end(writer_key)
        _shop_writers[writer_key].write(f"{level.upper()} [{module}] {name} {message}")
        _shop_log_seen[key] = now


class DailyLog:
    MAX_BYTES = 2 * 1024 * 1024

    def __init__(self, folder, source, secrets=()):
        self.folder = Path(folder)
        self.source = source
        self.secrets = secrets
        self.day = None

    def write(self, text, now=None, error=False):
        now = now or datetime.now(BEIJING)
        day = now.date()
        self.folder.mkdir(parents=True, exist_ok=True)
        if self.day != day:
            for path in self.folder.glob("*.log"):
                match = re.fullmatch(
                    r"(errors|collector|agent|panel|business|operations|results|(?:.+-)?shop-\d+)-(\d{4}-\d{2}-\d{2})(?:\.1)?\.log",
                    path.name,
                )
                if match:
                    try:
                        expired = datetime.strptime(match[2], "%Y-%m-%d").date() < day - timedelta(days=6)
                    except ValueError:
                        continue
                    if expired:
                        try:
                            path.unlink()
                        except OSError:
                            pass
            for name in (
                "collector.log",
                "agent.log",
                "panel.log",
                "collector.previous.log",
                "agent.previous.log",
                "panel.previous.log",
            ):
                path = self.folder / name
                try:
                    if path.exists() and datetime.fromtimestamp(path.stat().st_mtime, BEIJING).date() < day - timedelta(
                        days=6
                    ):
                        path.unlink()
                except OSError:
                    pass
            self.day = day
        safe = redact(text, self.secrets)[:16000]
        path = self.folder / f"{self.source}-{day.isoformat()}.log"
        if path.exists() and path.stat().st_size + len(safe.encode("utf-8")) + 32 > self.MAX_BYTES:
            path.replace(self.folder / f"{self.source}-{day.isoformat()}.1.log")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(now.strftime("%Y-%m-%d %H:%M:%S") + " " + safe.rstrip("\r\n") + "\n")
        if self.source != "errors" and (error or level_of(safe) == "error"):
            from .error_log import append

            append(self.folder, f"[{self.source}] {safe}", now)


class ProcessLog:
    def __init__(self, pipe, folder, source, secrets=()):
        import threading

        self.pipe = pipe
        self.writer = DailyLog(folder, source, secrets)
        self.thread = threading.Thread(target=self.run, daemon=True, name="log-" + source)
        self.thread.start()

    def run(self):
        traceback = False
        try:
            for line in iter(self.pipe.readline, b""):
                try:
                    text = line.decode("utf-8", errors="replace")
                    if re.search(r'"GET .+ HTTP/[\d.]+" 2\d\d', text):
                        continue
                    if re.search(r"\b(?:INFO|WARNING|WARN|ERROR|CRITICAL|DEBUG)\b", text) and not text[:1].isspace():
                        traceback = False
                    if "Traceback (most recent call last)" in text:
                        traceback = True
                    self.writer.write(text, error=traceback)
                    if traceback and text.strip() and not text[:1].isspace() and "Traceback" not in text:
                        traceback = False
                except OSError:
                    pass
        finally:
            self.pipe.close()

    def close(self):
        self.thread.join(timeout=3)


class FileTail:
    def __init__(self, path):
        self.path = Path(path)
        self.identity = None
        self.position = 0
        self.pending = b""

    def read(self, budget=65536):
        try:
            with self.path.open("rb") as stream:
                stat = self.path.stat()
                identity = (stat.st_dev, stat.st_ino)
                reset = self.identity != identity or stat.st_size < self.position
                skipped = False
                if reset:
                    self.position = max(0, stat.st_size - budget)
                    self.pending = b""
                    skipped = self.position > 0
                    self.identity = identity
                stream.seek(self.position)
                raw = stream.read(budget)
                self.position = stream.tell()
            if skipped:
                raw = raw.partition(b"\n")[2]
            chunks = (self.pending + raw).split(b"\n")
            self.pending = chunks.pop()
            # A malformed unbroken line must not grow the GUI's memory indefinitely.
            if len(self.pending) > budget:
                chunks.append(self.pending[:budget])
                self.pending = b""
            lines = [line.decode("utf-8", errors="replace").rstrip("\r") for line in chunks]
            if skipped:
                lines.insert(0, "仅载入最近日志；更早记录请打开日志目录查看")
            return lines
        except FileNotFoundError:
            return []


class DailyTail:
    def __init__(self, folder, source):
        self.folder = Path(folder)
        self.source = source
        self.tail = None

    def read(self):
        path = self.folder / f"{self.source}-{datetime.now(BEIJING):%Y-%m-%d}.log"
        if self.tail is None or self.tail.path != path:
            previous = self.tail.read() if self.tail else []
            self.tail = FileTail(path)
            return previous + self.tail.read()
        return self.tail.read()


class BusinessTail:
    def __init__(self, path):
        self.node_path = Path(path)
        self.folder = Path(path).parent / "log-spool"
        self.tails = {}

    def read(self):
        import json

        cutoff = (datetime.now(BEIJING).date() - timedelta(days=6)).isoformat()
        paths = sorted(p for p in self.folder.glob("????-??-??-*.jsonl") if p.name[:10] >= cutoff)
        # Initial GUI load shows today's tail; later polls follow each appended segment.
        paths = [p for p in paths if p.name[:10] == datetime.now(BEIJING).date().isoformat() or p.name in self.tails]
        live = {p.name for p in paths}
        self.tails = {k: v for k, v in self.tails.items() if k in live}
        result = []
        for path in paths:
            tail = self.tails.setdefault(path.name, FileTail(path))
            try:
                lines = tail.read()
            except OSError:
                continue
            for line in lines:
                try:
                    item = json.loads(line)
                    from .platforms import PLATFORMS

                    name, platform = item.get('shop_name'), item.get('platform')
                    if item.get('shop_id') and not name:
                        from .log_identity import identity

                        name, platform = identity(self.node_path, item['shop_id'])
                    shop = " · ".join(filter(None, [PLATFORMS.get(platform, ""), name]))
                    if not shop:
                        shop = f"店铺 {item['shop_id']}（名称暂不可用）" if item.get('shop_id') else '系统'
                    stamp = datetime.fromtimestamp(item["created"], BEIJING).strftime("%Y-%m-%d %H:%M:%S")
                    success = item.get("event") == "sync_success"
                    outcome = "成功 · " if success else "失败 · " if item.get("event") == "sync_error" else '限流等待 · ' if item.get('event') == 'sync_cooldown' else ""
                    result.append(
                        (
                            "success" if success else item["level"],
                            f"{stamp} [{item['module']}] {shop} {outcome}{item['message']}",
                        )
                    )
                except ValueError, KeyError, TypeError:
                    continue
        return result


class Reader:
    """One long-lived worker polls the log sources and hands batches to the UI through a bounded queue."""

    def __init__(self, tails, business, interval=1.0, archive=None):
        import queue
        import threading

        self.tails = tails
        self.business = business
        self.interval = interval
        self.archive = archive
        self.queue = queue.Queue(maxsize=8)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="collector-log-reader")

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stopped.set()

    def collect(self):
        rows = []
        for source, tail in self.tails.items():
            try:
                rows.extend((level_of(line), source, line) for line in tail.read() if line.strip())
            except OSError:
                pass
        if self.business is not None:
            try:
                rows.extend((level, "业务事件", line) for level, line in self.business.read())
            except sqlite3.Error:
                pass  # DB may still be initializing or briefly locked.
        if self.archive:
            for level, source, line in rows:
                if source == "业务事件":
                    try:
                        self.archive.write(level.upper() + " " + line)
                    except OSError:
                        pass
        return rows

    def run(self):
        import queue

        while not self.stopped.is_set():
            try:
                rows = self.collect()
            except Exception:
                rows = []
            while rows and not self.stopped.is_set():
                try:
                    self.queue.put(rows, timeout=0.5)
                    break
                except queue.Full:
                    pass  # UI is busy; keep the batch instead of dropping lines.
            self.stopped.wait(self.interval)

    def drain(self):
        import queue

        rows = []
        while True:
            try:
                rows.extend(self.queue.get_nowait())
            except queue.Empty:
                return rows


class LogPanel:
    """All Tk operations stay on the main thread; disk reads happen in one long-lived worker."""

    LIMIT = 3000
    LEVELS = {"错误": "error", "警告": "warning", "成功": "success", "信息": "info", "调试": "debug"}

    def __init__(self, parent, data, secrets, mode="runtime"):
        import tkinter as tk
        from tkinter import ttk

        self.data = Path(data)
        self.secrets = secrets
        self.rows = deque(maxlen=self.LIMIT)
        self.last_status = None
        self.tails = {
            name: DailyTail(self.data / "launcher-logs", file)
            for name, file in [("采集服务", "collector"), ("中心连接", "agent"), ("控制面板", "panel")]
        }
        self.business = BusinessTail(self.data / "shops.sqlite3")
        self.tails["任务结果"] = DailyTail(self.data / "launcher-logs", "results")
        if mode == "operations":
            self.tails = {"操作步骤": DailyTail(self.data / "launcher-logs", "operations")}
            self.business = None
        self.frame = ttk.LabelFrame(parent, text="操作日志" if mode == "operations" else "运行日志", padding=10)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(1, weight=1)
        bar = ttk.Frame(self.frame)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.filter = tk.StringVar(value="全部")
        self.follow = tk.BooleanVar(value=True)
        self.pause = tk.BooleanVar(value=False)
        combo = ttk.Combobox(
            bar,
            textvariable=self.filter,
            values=["全部", "错误", "警告", "成功", "信息", "调试"],
            width=7,
            state="readonly",
        )
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _: self.render())
        ttk.Checkbutton(bar, text="自动滚动", variable=self.follow).pack(side="left", padx=8)
        ttk.Checkbutton(bar, text="暂停显示", variable=self.pause, command=self.render).pack(side="left")
        ttk.Button(bar, text="清空显示", command=self.clear).pack(side="right")
        self.text = tk.Text(
            self.frame,
            wrap="word",
            height=6,
            bg="#f7f9fc",
            fg="#234168",
            relief="flat",
            padx=10,
            pady=10,
            font=("Microsoft YaHei UI", 9),
            state="disabled",
        )
        self.text.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self.frame, command=self.text.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scroll.set)
        for name, color in {
            "error": "#bf2323",
            "warning": "#966000",
            "success": "#157347",
            "info": "#234e86",
            "debug": "#6b7280",
        }.items():
            self.text.tag_configure(name, foreground=color)
        ttk.Label(
            self.frame,
            text="错误红色 · 警告橙色 · 成功绿色 · 信息蓝色 · 调试灰色 ｜ 界面保留最近 3000 条，日志文件按天保存 · 最近 7 天",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.reader = Reader(self.tails, self.business).start()
        self.frame.bind("<Destroy>", lambda event: self.reader.stop() if event.widget is self.frame else None)

    def status(self, message, level=None):
        if not message or message == self.last_status:
            return
        self.last_status = message
        safe = redact(message, self.secrets())
        try:
            DailyLog(self.data / "launcher-logs", "panel").write((level or level_of(safe)).upper() + " " + safe)
        except OSError:
            self.add(
                [
                    (
                        level or level_of(safe),
                        "控制面板",
                        datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S") + " " + safe,
                    )
                ]
            )

    def clear(self):
        self.rows.clear()
        self.render(force=True)

    def visible(self, rows):
        selected = self.LEVELS.get(self.filter.get())
        return [row for row in rows if not selected or row[0] == selected]

    def write(self, rows):
        for level, source, line in rows:
            self.text.insert("end", f"[{source}] " + line.replace("\n", " ") + "\n", level)

    def render(self, force=False):
        """Full redraw, only for filter/clear/unpause; new lines go through add()."""
        if self.pause.get() and not force:
            return
        position = self.text.yview()[0]
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.write(self.visible(self.rows))
        self.text.configure(state="disabled")
        if self.follow.get():
            self.text.see("end")
        else:
            self.text.yview_moveto(position)

    def add(self, rows):
        if not rows:
            return
        self.rows.extend(rows)
        if self.pause.get():
            return
        shown = self.visible(rows[-self.LIMIT :])
        if not shown:
            return
        self.text.configure(state="normal")
        self.write(shown)
        excess = int(self.text.index("end-1c").split(".")[0]) - 1 - self.LIMIT
        if excess > 0:
            self.text.delete("1.0", f"{excess + 1}.0")
        self.text.configure(state="disabled")
        if self.follow.get():
            self.text.see("end")

    def tick(self):
        rows = [(level, source, redact(line, self.secrets())) for level, source, line in self.reader.drain()]
        self.add(rows)
