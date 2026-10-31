import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import importlib.util
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

QT = importlib.util.find_spec("PySide6") is not None


@unittest.skipUnless(QT, "Optional Qt runtime not installed")
class QtConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from backend.collector_qt import CollectorWindow

        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.runner = Mock(running=False, restart_requested=False, port=8001, restarts=0)
        with patch("backend.collector_qt.core.load_config", side_effect=FileNotFoundError):
            self.window = CollectorWindow(self.root, self.runner, start_timers=False)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.closing = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.folder.cleanup()

    def drain(self):
        deadline = time.monotonic() + 3
        while self.window.messages.empty() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.window.tick()
        self.app.processEvents()

    def test_small_window_keeps_log_area_and_tabs_visible(self):
        self.window.resize(720, 520)
        self.app.processEvents()
        self.assertEqual(self.window.tabs.count(), 3)
        self.assertEqual(self.window.tabs.tabText(0), "操作日志")
        self.assertGreater(self.window.operations.text.height(), 100)
        self.assertLessEqual(self.window.tabs.geometry().bottom(), self.window.centralWidget().height())

    def test_log_is_bounded_redacted_searchable_and_pause_preserves_results(self):
        log = self.window.runtime
        log.secrets = lambda: ["private-value"]
        log.pause.setChecked(True)
        log.add([("error", "任务结果", f"shop-{i} private-value 失败") for i in range(4000)])
        self.assertEqual(len(log.rows), 3000)
        self.assertEqual(log.text.toPlainText(), "")
        log.pause.setChecked(False)
        self.assertNotIn("private-value", log.text.toPlainText())
        self.assertLessEqual(log.text.document().blockCount(), 3000)
        log.search.setText("shop-3999")
        self.assertIn("shop-3999", log.text.toPlainText())
        self.assertNotIn("shop-3998", log.text.toPlainText())
        log.clear()
        self.assertEqual(len(log.rows), 0)

    def test_worker_never_blocks_gui_and_duplicate_action_is_rejected(self):
        import threading

        gate = threading.Event()
        self.assertTrue(self.window.work(lambda: gate.wait(2)))
        self.assertFalse(self.window.work(lambda: self.fail("duplicate work")))
        self.window.tabs.setCurrentIndex(1)
        self.app.processEvents()
        self.assertEqual(self.window.tabs.currentIndex(), 1)
        gate.set()
        self.drain()
        self.assertFalse(self.window.busy)

    def test_stop_cancels_pending_enrollment_and_does_not_restart(self):
        self.window.pending = {"id": "pending"}
        self.runner.stop.return_value = None
        self.window.stop_service()
        self.drain()
        self.assertIsNone(self.window.pending)
        self.assertTrue(self.window.manual_stopped)
        self.runner.stop.assert_called_once()
        self.runner.start.assert_not_called()

    def test_close_minimizes_without_stopping_service(self):
        self.window.close()
        self.app.processEvents()
        self.assertTrue(self.window.isMinimized())
        self.runner.stop.assert_not_called()

    def test_concurrency_editor_submits_only_changed_fields(self):
        from backend.collector_qt import ConcurrencyDialog

        with patch(
            "backend.collector_concurrency.request", return_value={"max_concurrent": 20, "shop_concurrent": 3}
        ) as request:
            dialog = ConcurrencyDialog(self.window)
            deadline = time.monotonic() + 3
            while dialog.results.empty() and time.monotonic() < deadline:
                time.sleep(0.01)
            dialog.poll()
            dialog.values["shop_concurrent"].setText("4")
            request.return_value = {"max_concurrent": 20, "shop_concurrent": 4}
            dialog.submit()
            deadline = time.monotonic() + 3
            while dialog.results.empty() and time.monotonic() < deadline:
                time.sleep(0.01)
            dialog.poll()
            self.assertEqual(request.call_args.args[2], {"shop_concurrent": 4})
            dialog.close()

    def test_enrollment_approval_reuses_existing_runner(self):
        config = {"url": "http://127.0.0.1:8010", "token": "x" * 40, "enrollment": True, "name": "test", "auto": False}
        self.window.url.setText(config["url"])
        with (
            patch("backend.collector_qt.core.load_config", side_effect=FileNotFoundError),
            patch("backend.collector_enrollment.configuration", return_value=config),
            patch(
                "backend.collector_enrollment.enroll",
                return_value={"state": "approved", "id": "node-1", "name": "审批节点"},
            ),
            patch("backend.collector_qt.core.save_config"),
            patch("backend.collector_qt.core.startup"),
        ):
            self.window.start_service()
            self.drain()
        self.runner.start.assert_called_once()
        self.assertEqual(self.runner.start.call_args.args[0]["id"], "node-1")
        self.assertIsNone(self.window.pending)

    def test_ui_fingerprint_includes_qt_changes(self):
        from backend.collector_restart import fingerprint

        before = fingerprint(self.root)
        (self.root / "backend").mkdir()
        (self.root / "backend/collector_qt.py").write_text("new")
        self.assertNotEqual(before, fingerprint(self.root))

    def test_timing_tab_updates_existing_step_without_losing_selection(self):
        self.assertEqual(self.window.tabs.tabText(2), '采集状态')
        row={'shop_id':1,'shop_name':'测试店铺','master_id':'home','module':'首页','phase':'running','stage':'查询中',
             'steps':[{'cache_key':'orders:x','name':'今日订单','stage':'查询中','timing_seconds':{'query':1.5},
                       'timing_current':{'reason':'查询订单','elapsed_seconds':1.5}}]}
        view=self.window.timing
        view.render([row])
        self.assertEqual(len(view.items), 1)
        view.items['shop-1'].setExpanded(True)
        view.items['1:home'].setExpanded(True)
        item=view.items['1:home:orders:x'];view.tree.setCurrentItem(item)
        row['steps'][0]['timing_seconds']['query']=2.5
        view.render([row])
        self.assertIs(view.tree.currentItem(),item)
        self.assertEqual(item.text(5),'2.5秒')
        view.render([])
        self.assertEqual(view.tree.topLevelItemCount(),0)

    def test_timing_only_update_triggers_console_restart_fingerprint(self):
        from backend.collector_restart import fingerprint
        before = fingerprint(self.root)
        (self.root / 'backend').mkdir(exist_ok=True)
        (self.root / 'backend/collector_timing.py').write_text('changed')
        self.assertNotEqual(before, fingerprint(self.root))

    def test_lazy_shop_heading_uses_actual_progress_display_identity(self):
        self.window.timing.render([{'shop_id': 17, 'shop_label': '拼多多 · 名称测试店',
            'master_id': 'home', 'module': '首页', 'steps': [], 'phase': 'waiting'}])
        self.assertEqual(self.window.timing.items['shop-17'].text(0), '拼多多 · 名称测试店')
