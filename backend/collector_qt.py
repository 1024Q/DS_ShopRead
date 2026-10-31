"""Qt Widgets console. Workers own IO; only the GUI thread touches widgets.

Collection, enrollment, credential storage and signed updates reuse existing services.
Qt is an optional runtime upgrade so older portable installations remain startable.
"""

import logging
from collections import deque
from pathlib import Path
import os
import queue
import sys
import threading
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QCheckBox,
    QTabWidget,
    QPlainTextEdit,
    QComboBox,
    QGroupBox,
    QMessageBox,
    QDialog,
    QFormLayout,
)

from . import collector_launcher as core
from .collector_logs import Reader, DailyTail, BusinessTail, DailyLog, redact, level_of
from .collector_update import read_json as _read_json


def read_json(path, default=None):
    # A transient status-file read failure must not terminate the UI timer.
    # The updater itself keeps its strict validation and rollback behavior.
    try:
        return _read_json(path, default)
    except OSError, ValueError:
        return default


STYLE = """
QWidget { font-family: "Microsoft YaHei UI"; font-size: 13px; color: #24364b; }
QMainWindow { background: #f3f5f8; }
QGroupBox { background: white; border: 1px solid #dde4ed; border-radius: 10px; margin-top: 10px; padding: 10px 12px 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
QPushButton { border: 1px solid #d6deea; border-radius: 6px; background: white; padding: 7px 14px; }
QPushButton:hover { border-color: #3478ed; background: #edf4ff; }
QPushButton:disabled { color: #98a4b5; background: #f4f6f9; }
QPushButton#service { background: #3478ed; color: white; border: none; min-width: 90px; }
QLineEdit, QComboBox { border: 1px solid #d6deea; border-radius: 5px; padding: 6px; background: white; }
QPlainTextEdit { border: 1px solid #e0e6ee; border-radius: 6px; background: #fbfcfe; padding: 6px; }
QTabWidget::pane { border: 1px solid #dde4ed; background: white; border-radius: 8px; }
QTabBar::tab { padding: 10px 22px; background: #e9eef5; margin-right: 4px; }
QTabBar::tab:selected { background: white; color: #2468d4; border-bottom: 2px solid #3478ed; }
QStatusBar { background: #e8eef8; }
"""


class LogView(QWidget):
    LIMIT = 3000
    LEVELS = {"全部": None, "错误": "error", "警告": "warning", "成功": "success", "信息": "info", "调试": "debug"}
    COLORS = {"error": "#bf2323", "warning": "#966000", "success": "#157347", "info": "#234e86", "debug": "#6b7280"}

    def __init__(self, directory, secrets, operations=False, start_reader=True):
        super().__init__()
        self.directory, self.secrets = Path(directory), secrets
        self.rows = deque(maxlen=self.LIMIT)
        self.dirty_render = False
        self.last_render = 0
        self.last_status = None
        self.reader = None
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.addItems(self.LEVELS)
        self.follow = QCheckBox("自动滚动")
        self.follow.setChecked(True)
        self.pause = QCheckBox("暂停显示")
        self.search = QLineEdit()
        self.search.setPlaceholderText("查找店铺、模块或原因")
        clear = QPushButton("清空显示")
        clear.clicked.connect(self.clear)
        for w in (self.filter, self.follow, self.pause, self.search, clear):
            bar.addWidget(w)
        layout.addLayout(bar)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(self.LIMIT)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(self.text, 1)
        note = QLabel("仅清空界面 · 文件保留最近 7 天 · 错误另存 errors 日志")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.filter.currentTextChanged.connect(self.render)
        self.search.textChanged.connect(self.render)
        self.pause.toggled.connect(lambda paused: None if paused else self.render())
        if start_reader:
            tails = (
                {"操作步骤": DailyTail(self.directory / "launcher-logs", "operations")}
                if operations
                else {
                    name: DailyTail(self.directory / "launcher-logs", source)
                    for name, source in [
                        ("采集服务", "collector"),
                        ("中心连接", "agent"),
                        ("控制面板", "panel"),
                        ("任务结果", "results"),
                    ]
                }
            )
            self.reader = Reader(tails, None if operations else BusinessTail(self.directory / "shops.sqlite3")).start()

    def close_reader(self):
        if self.reader:
            self.reader.stop()

    def status(self, message, level=None):
        if not message or message == self.last_status:
            return
        self.last_status = message
        safe = redact(message, self.secrets())
        try:
            DailyLog(self.directory / "launcher-logs", "panel").write((level or level_of(safe)).upper() + " " + safe)
        except OSError:
            self.add([(level or level_of(safe), "控制面板", safe)])

    def visible(self, rows):
        level = self.LEVELS[self.filter.currentText()]
        query = self.search.text().casefold().strip()
        from .log_summary import compact
        return compact([r for r in rows if (not level or r[0] == level) and (not query or query in r[2].casefold())])

    def append(self, rows):
        bar = self.text.verticalScrollBar()
        position = bar.value()
        cursor = QTextCursor(self.text.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()
        for level, source, line in rows:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(self.COLORS.get(level, "#234e86")))
            cursor.insertText(f"[{source}] {line}\n", fmt)
        cursor.endEditBlock()
        bar.setValue(bar.maximum() if self.follow.isChecked() else position)

    def add(self, rows):
        safe = [(level, source, redact(line, self.secrets())) for level, source, line in rows]
        was_dirty = self.dirty_render
        self.rows.extend(safe)
        if not safe:return
        self.dirty_render = True
        if self.isVisible() and not self.pause.isChecked():
            if was_dirty or any(r[0] in ('error', 'warning') for r in safe):
                self.render()
            else:
                self.append(self.visible(safe[-self.LIMIT :]))
                self.dirty_render = False

    def render(self, *_):
        if self.pause.isChecked():
            return
        self.text.clear()
        self.append(self.visible(self.rows))
        self.dirty_render = False

    def clear(self):
        self.rows.clear()
        self.text.clear()

    def tick(self):
        if self.reader:
            self.add(self.reader.drain())
        if self.dirty_render and self.isVisible() and not self.pause.isChecked():self.render()


class ConcurrencyDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        from .collector_concurrency import FIELDS

        self.owner = parent
        self.original = None
        self.results = queue.Queue()
        self.busy = False
        self.setWindowTitle("本机采集参数")
        self.resize(460, 240)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.values = {}
        for key, label in FIELDS.items():
            field = QLineEdit()
            self.values[key] = field
            form.addRow(label, field)
        layout.addLayout(form)
        self.message = QLabel("正在读取本机配置…")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        actions = QHBoxLayout()
        self.reload = QPushButton("重新读取")
        self.save = QPushButton("保存修改项")
        self.reload.clicked.connect(lambda: self.request(None))
        self.save.clicked.connect(self.submit)
        actions.addWidget(self.reload)
        actions.addWidget(self.save)
        layout.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(100)
        self.request(None)

    def request(self, patch):
        if self.busy:
            return
        from .collector_concurrency import request

        self.busy = True
        self.controls(False)
        self.message.setText("正在保存并核实…" if patch else "正在读取本机配置…")
        directory, port = self.owner.directory, self.owner.runner.port

        def work():
            try:
                self.results.put((request(directory, port, patch), None, bool(patch)))
            except Exception as error:
                self.results.put(
                    (None, str(error) if isinstance(error, ValueError) else "请求失败，请重新读取确认保存结果。", False)
                )

        threading.Thread(target=work, daemon=True).start()

    def controls(self, enabled):
        self.reload.setEnabled(enabled)
        self.save.setEnabled(enabled and self.original is not None)
        for field in self.values.values():
            field.setEnabled(enabled and self.original is not None)

    def submit(self):
        from .collector_concurrency import changes

        try:
            patch = changes({k: v.text() for k, v in self.values.items()}, self.original)
        except ValueError as error:
            self.message.setText(str(error))
            return
        if not patch:
            self.message.setText("没有修改项")
            return
        self.request(patch)

    def poll(self):
        try:
            config, error, saved = self.results.get_nowait()
        except queue.Empty:
            return
        self.busy = False
        if config:
            self.original = config
            for key, field in self.values.items():
                field.setText(str(config[key]))
            self.message.setText(
                "已保存，并确认本机读取值一致。" if saved else "已读取当前配置；同店同业务模块仍互斥。"
            )
        else:
            self.message.setText(error)
        self.controls(True)


class CollectorWindow(QMainWindow):
    def __init__(self, root=core.ROOT, runner=None, start_timers=True):
        super().__init__()
        from .collector_restart import fingerprint

        self.root = Path(root)
        self.directory = self.root / ".data"
        self.runner = runner or core.Runner(self.root)
        self.messages = queue.Queue()
        self.busy = False
        self.polling = False
        self.connected = None
        self.pending = None
        self.manual_stopped = False
        self.center = [None, 0.0]
        self.last = {}
        self.quiet = None
        self.version = "手动部署"
        self.version_stamp = None
        self.loaded_ui = fingerprint(self.root)
        self.restart_ui = False
        self.restart_minimized = False
        self.restart_delay = 60
        self.closing = False
        self.config = {}
        self.saved_token = ""
        self.setStyleSheet(STYLE)
        self.resize(1060, 760)
        self.setMinimumSize(700, 500)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(18, 14, 18, 10)
        layout.setSpacing(12)
        self.setCentralWidget(body)
        heading = QHBoxLayout()
        self.title = QLabel("采集服务 · 已登录 — 家")
        self.title.setFont(QFont("Microsoft YaHei UI", 16, QFont.Weight.Bold))
        heading.addWidget(self.title, 1)
        self.start = QPushButton("启动")
        self.start.setObjectName("service")
        self.start.clicked.connect(self.toggle)
        heading.addWidget(self.start)
        layout.addLayout(heading)
        self.connection = QGroupBox("连接设置")
        connection_layout = QVBoxLayout(self.connection)
        connection_bar = QHBoxLayout()
        self.expand = QPushButton("展开连接设置")
        self.node = QLabel("节点：尚未识别")
        self.node.setWordWrap(True)
        connection_bar.addWidget(self.expand)
        connection_bar.addWidget(self.node, 1)
        connection_layout.addLayout(connection_bar)
        self.settings = QWidget()
        form = QFormLayout(self.settings)
        form.setContentsMargins(0, 0, 0, 0)
        self.url = QLineEdit()
        self.url.setPlaceholderText("http://中心地址:8010")
        form.addRow("中心地址", self.url)
        self.auto = QCheckBox("登录 Windows 后自动启动")
        form.addRow(self.auto)
        hint = QLabel("密钥自动生成并加密保存；首次接入由中心审批。关闭窗口后继续采集。")
        hint.setWordWrap(True)
        form.addRow(hint)
        connection_layout.addWidget(self.settings)
        self.settings.setVisible(not (self.directory / "collector-connection.local.json").exists())
        self.expand.clicked.connect(self.toggle_settings)
        layout.addWidget(self.connection)
        bar = QHBoxLayout()
        self.update_button = QPushButton("检查更新")
        self.parameters = QPushButton("采集参数")
        logs = QPushButton("打开日志")
        quit_button = QPushButton("退出")
        for w in (self.update_button, logs, self.parameters):
            bar.addWidget(w)
        bar.addStretch()
        bar.addWidget(quit_button)
        layout.addLayout(bar)
        self.update_button.clicked.connect(self.check_update)
        self.parameters.clicked.connect(self.open_parameters)
        logs.clicked.connect(self.open_logs)
        quit_button.clicked.connect(self.quit_app)
        self.tabs = QTabWidget()
        secrets = lambda: [self.saved_token, (self.connected or {}).get("token", "")]
        self.operations = LogView(self.directory, secrets, True, start_reader=start_timers)
        self.runtime = LogView(self.directory, secrets, start_reader=start_timers)
        self.tabs.addTab(self.operations, "操作日志")
        self.tabs.addTab(self.runtime, "运行日志")
        from .collector_timing import TimingView
        self.timing = TimingView(self.directory, self.runner.port, start_timers=start_timers)
        self.tabs.addTab(self.timing, '采集状态')
        layout.addWidget(self.tabs, 1)
        self.status = QLabel("填写中心地址后申请接入，中心批准后自动启动")
        self.status.setWordWrap(True)
        self.statusBar().addWidget(self.status, 1)
        for label in (self.title, self.node, self.status):
            label.setTextFormat(Qt.TextFormat.PlainText)
        try:
            config = core.load_config()
            self.url.setText(config["url"])
            self.saved_token = config["token"]
            self.auto.setChecked(config.get("auto", False))
            self.node.setText("节点：" + config.get("name", ""))
        except FileNotFoundError:
            pass
        except Exception:
            self.status.setText("配置无法解密，请使用原 Windows 用户；不会覆盖原凭据。")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        if start_timers:
            self.timer.start(500)
            self.runtime.status("PySide6 控制台已打开")
            if "--configure" not in sys.argv and self.saved_token:
                QTimer.singleShot(200, self.start_service)

    def toggle_settings(self):
        self.settings.setVisible(not self.settings.isVisible())
        self.expand.setText("收起连接设置" if self.settings.isVisible() else "展开连接设置")

    def work(self, fn, quiet=False):
        if self.busy:
            return False
        self.busy = True
        self.start.setEnabled(False)
        if not quiet:
            self.status.setText("正在执行操作，请稍候")
            self.runtime.status("正在执行操作，请稍候")

        def job():
            try:
                self.messages.put(("quiet" if quiet else "done", fn()))
            except Exception as error:
                self.messages.put(
                    ("error", str(error) if isinstance(error, ValueError) else "操作失败，请查看运行日志和错误日志。")
                )
                self.runtime_error(error)

        threading.Thread(target=job, daemon=True, name="console-operation").start()
        return True

    def runtime_error(self, error):
        import traceback

        try:
            DailyLog(
                self.directory / "launcher-logs", "panel", [self.saved_token, (self.connected or {}).get("token", "")]
            ).write("ERROR " + "".join(traceback.format_exception(error)), error=True)
        except OSError:
            pass

    def toggle(self):
        if self.runner.running or self.pending:
            self.stop_service()
        else:
            self.start_service()

    def start_service(self, quiet=False):
        if self.busy:
            return
        self.manual_stopped = False
        entered_url, autostart = self.url.text(), self.auto.isChecked()

        def task():
            from .collector_enrollment import configuration, enroll, code

            address = core.center_url(entered_url)
            try:
                current = core.load_config()
            except FileNotFoundError:
                current = None
            config = configuration(address, current, autostart)
            if config != current:
                core.save_config(config)
                core.startup(autostart)
            if config.get("enrollment"):
                self.pending = config
                self.last["enrollment"] = time.monotonic()
                value = enroll(config, read_json(self.root / "collector-version.json", {}).get("version", ""))
                if value["state"] != "approved":
                    if value["state"] != "pending":
                        self.pending = None
                    labels = {
                        "pending": "等待中心确认",
                        "rejected": "中心已拒绝接入",
                        "revoked": "节点权限已撤销",
                        "expired": "申请已过期",
                    }
                    return labels[value["state"]] + " · 机器 " + config["name"] + " · 核对码 " + code(config)
                config.update(id=value["id"], name=value["name"], enrollment=False)
                core.save_config(config)
                self.pending = None
            else:
                identity = core.identify(address, config["token"])
                config.update(id=identity["id"], name=identity["name"])
            self.connected = config
            try:
                self.runner.start(config)
            except Exception:
                upgrade = read_json(self.directory / "updates/status.json", {})
                if upgrade.get("resume_probation"):
                    from .collector_update import revert

                    return revert(
                        self.runner, config, upgrade.get("version"), "新版控制台接续启动失败，已回退并恢复旧版服务"
                    )
                raise
            core.save_config(config)
            core.startup(autostart)
            return "采集服务已启动，正在建立中心连接"

        self.work(task, quiet)

    def stop_service(self):
        if self.busy:
            return
        self.manual_stopped = True
        self.pending = None
        self.work(lambda: self.runner.stop() or "采集服务已停止，配置已保留")

    def check_update(self):
        from .collector_update import update

        self.work(lambda: update(self.runner, force=True))

    def open_parameters(self):
        if not self.runner.running:
            return
        dialog = getattr(self, "parameters_dialog", None)
        if dialog and dialog.isVisible():
            dialog.raise_()
            dialog.activateWindow()
            return
        self.parameters_dialog = ConcurrencyDialog(self)
        self.parameters_dialog.show()

    def open_logs(self):
        folder = self.directory / "launcher-logs"
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    def quit_app(self):
        if self.busy:
            return
        if (
            self.runner.running
            and QMessageBox.question(self, "退出", "退出将停止采集服务，是否继续？") != QMessageBox.StandardButton.Yes
        ):
            return

        def stop():
            self.runner.stop()
            self.messages.put(("exit", None))

        self.work(stop)

    def closeEvent(self, event):
        if self.closing:
            event.accept()
        else:
            event.ignore()
            self.showMinimized()

    def finish(self, restart=False):
        self.restart_ui = restart
        self.restart_minimized = self.isMinimized()
        self.closing = True
        self.timer.stop()
        self.operations.close_reader()
        self.runtime.close_reader()
        self.close()
        QApplication.instance().quit()

    def due(self, key, seconds):
        now = time.monotonic()
        if now - self.last.get(key, -10000) < seconds:
            return False
        self.last[key] = now
        return True

    def poll_health(self):
        if self.polling or not self.connected:
            return
        self.polling = True
        config = dict(self.connected)
        port = self.runner.port

        def job():
            try:
                from .collector_overview import read_snapshot

                try:
                    self.messages.put(("snapshot", read_snapshot(self.directory, port)[1]))
                except Exception:
                    logging.getLogger(__name__).debug("读取采集概况快照失败", exc_info=True)
                if self.due("identity", 15):
                    try:
                        identity = core.identify(config["url"], config["token"])
                        self.center[:] = [bool(identity.get("connected")), time.time()]
                        if identity["name"] != config.get("name"):
                            core.save_config({**config, "name": identity["name"]})
                            self.messages.put(("identity", identity))
                        self.messages.put(
                            ("status", ("中心已连接" if identity.get("connected") else "正在连接中心") + " · 运行正常")
                        )
                    except Exception:
                        self.center[:] = [None, time.time()]
                        self.messages.put(("status", "中心暂不可达，正在自动重连；本地采集继续运行"))
            finally:
                self.messages.put(("poll_done", None))

        threading.Thread(target=job, daemon=True, name="console-health").start()

    def tick(self):
        interval = 1500 if self.isMinimized() else 500
        if self.timer.interval() != interval:
            self.timer.setInterval(interval)
        if self.runner.restart_requested:
            self.finish(True)
            return
        while not self.messages.empty():
            kind, value = self.messages.get_nowait()
            if kind == "exit":
                self.finish()
                return
            if kind == "restart":
                self.finish(True)
                return
            if kind == "poll_done":
                self.polling = False
                continue
            if kind == "snapshot":
                self.config = value
                continue
            if kind == "identity":
                if self.connected and self.connected.get("id") == value.get("id"):
                    self.connected["name"] = value["name"]
                continue
            if kind == "status":
                if not self.busy and self.runner.running:
                    delivery = self.config.get("delivery", {})
                    rejected = sum(v.get("rejected_count", 0) for v in delivery.values() if isinstance(v, dict))
                    if rejected:
                        value += f" · 中心拒收 {rejected} 份，数据保留本地（详见运行日志）"
                    pending = sum(v.get('pending', 0) for v in delivery.values() if isinstance(v, dict))
                    dates = [v['oldest_pending'] for v in delivery.values() if isinstance(v, dict) and v.get('oldest_pending')]
                    if pending:
                        value += f' · 本地已保存，等待上传 {pending} 份'
                        if dates:
                            from datetime import datetime
                            value += ' · 最早积压 ' + datetime.fromtimestamp(min(dates)).strftime('%m-%d %H:%M:%S')
                    reasons = list(dict.fromkeys(v.get('error') for v in delivery.values() if isinstance(v, dict) and v.get('error')))
                    if reasons:value += ' · ' + '；'.join(reasons)[:180]
                    if self.status.text() != value:self.status.setText(value)
                continue
            self.busy = False
            if value:
                self.status.setText(str(value))
                if kind != "quiet" or value != self.quiet:
                    self.runtime.status(str(value), "error" if kind == "error" else None)
                if kind == "quiet":
                    self.quiet = value
        active = self.runner.running
        self.start.setEnabled(not self.busy)
        self.start.setText("停止" if active or self.pending else "启动")
        self.update_button.setEnabled(active and not self.busy)
        self.parameters.setEnabled(active and not self.busy)
        self.url.setEnabled(not active and not self.busy)
        self.auto.setEnabled(not active and not self.busy)
        shops = self.config.get("shops", {}) if active else {}
        name = (self.connected or {}).get("name", "采集服务")
        title = f"{name} · 已登录 {shops.get('logged_in', 0)} 家店铺"
        if shops.get("sleeping"):
            title += f" · 休眠 {shops['sleeping']} 家"
        if self.title.text() != title:
            self.title.setText(title)
            self.setWindowTitle(title + " · 多店铺数据工作台")
        if self.due("version", 5):
            path = self.root / 'collector-version.json'
            try:
                stamp = (path.stat().st_mtime_ns, path.stat().st_size)
            except OSError:
                stamp = None
            if stamp != self.version_stamp:
                self.version = read_json(path, {}).get('version', '手动部署')
                self.version_stamp = stamp
        if self.connected:
            text = '节点：' + name + ' · 版本 ' + self.version + ' · PySide6'
            if self.node.text() != text:self.node.setText(text)
        self.operations.tick()
        self.runtime.tick()
        if self.busy:
            return
        from .collector_update import probation, recover, update, retry_delay

        journal = (self.directory / "updates/journal.json").exists()
        if not active:
            if self.pending and not self.manual_stopped and self.due("enrollment", 15):
                self.start_service(True)
            elif (
                not self.manual_stopped
                and self.connected
                and self.due("recover", 60)
                and read_json(self.directory / "updates/status.json", {}).get("stopped")
            ):
                self.work(lambda: recover(self.runner), True)
            return
        if self.due("health", 5):
            self.poll_health()
        if journal and self.due("trial", 15):
            self.work(lambda: probation(self.runner, *self.center), True)
            return
        from .collector_restart import needed, prepare

        if self.due("restart", self.restart_delay) and needed(self.root, self.loaded_ui):

            def restart():
                try:
                    prepare(self.runner)
                except Exception:
                    self.restart_delay = min(1800, self.restart_delay * 2)
                    raise
                self.messages.put(("restart", None))

            self.work(restart, True)
            return
        if (
            not journal
            and (self.root / "collector-update-public.pem").exists()
            and self.due("update", max(60, retry_delay(self.root)))
        ):
            self.work(lambda: update(self.runner), True)
            return
        if self.due("monitor", 2):

            def monitor():
                try:
                    self.runner.monitor()
                except Exception:
                    self.runner.stop()
                    raise

            self.work(monitor, True)


def main():
    import ctypes
    import hashlib
    from ctypes import wintypes

    if os.name != "nt":
        raise RuntimeError("此启动器用于 Windows x64")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("ECMP Collector")
    app.setStyle("Fusion")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    mutex = kernel.CreateMutexW(
        None, False, "Local\\ECMPCollector-" + hashlib.sha256(str(core.ROOT).lower().encode()).hexdigest()
    )
    if ctypes.get_last_error() == 183:
        QMessageBox.information(None, "采集服务", "此目录的控制面板已经打开，请从任务栏恢复。")
        kernel.CloseHandle(mutex)
        return
    window = None
    try:
        from .collector_update import startup

        try:
            startup(core.ROOT)
        except Exception:
            QMessageBox.critical(None, "升级恢复失败", "上次升级恢复失败，请保留 .data/updates 并联系管理员。")
            return
        window = CollectorWindow()
        screen = app.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            window.resize(min(1060, available.width() - 40), min(760, available.height() - 60))
        window.show()
        if "--minimized" in sys.argv:
            window.showMinimized()
        app.exec()
    finally:
        if window:
            window.operations.close_reader()
            window.runtime.close_reader()
            window.runner.stop()
        kernel.CloseHandle(mutex)
    if window and window.restart_ui:
        from .collector_restart import launch

        launch(core.ROOT, window.restart_minimized)
