"""Nonblocking, read-only Qt collection step/timing view."""
import queue
import threading
from pathlib import Path
import httpx
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QTreeWidget, QTreeWidgetItem

PHASES = ('slot_wait', 'browser_start', 'page_load', 'query', 'database')


class TimingView(QWidget):
    def __init__(self, directory, port, start_timers=True):
        super().__init__()
        self.directory, self.port = Path(directory), port
        self.results, self.items = queue.Queue(), {}
        self.busy = False
        self.rows = []
        self.dispatch = {}
        layout = QVBoxLayout(self)
        self.note = QLabel('每店到期触发，按现有名额执行；诊断仅保存到文件。展开主任务查看子步骤。')
        self.note.setWordWrap(True); layout.addWidget(self.note)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['店铺 / 主任务 / 子步骤', '当前步骤 / 等待原因', '等名额', '浏览器', '页面', '接口', '入库', '上传确认'])
        self.tree.setColumnWidth(0, 250); self.tree.setColumnWidth(1, 300)
        self.tree.itemExpanded.connect(lambda _: self.render(self.rows))
        self.tree.itemCollapsed.connect(lambda _: self.render(self.rows))
        self.tree.setUniformRowHeights(True)
        self.tree.setHorizontalScrollMode(QTreeWidget.ScrollMode.ScrollPerPixel)
        layout.addWidget(self.tree, 1)
        self.timer = QTimer(self); self.timer.timeout.connect(self.poll)
        self.ticks = 0
        if start_timers:self.timer.start(500)

    def poll(self):
        try:
            rows, error = self.results.get_nowait()
            self.busy = False
            if error:self.note.setText('采集状态暂不可用，稍后自动刷新；日志保留在文件中。')
            else:
                self.dispatch = rows.get('dispatch', {})
                self.render(rows.get('master_tasks', []))
        except queue.Empty:pass
        self.ticks += 1
        if self.busy or not self.isVisible() or self.ticks % 10: return
        self.busy = True
        def read():
            try:
                token = (self.directory / 'api.key').read_text().strip()
                r = httpx.get(f'http://127.0.0.1:{self.port}/api/system/progress',
                    headers={'Authorization': 'Bearer ' + token}, trust_env=False, timeout=8)
                r.raise_for_status(); self.results.put((r.json(), None))
            except Exception as e:self.results.put(([], type(e).__name__))
        threading.Thread(target=read, daemon=True, name='console-timing').start()

    def render(self, rows):
        self.rows = rows
        seen = set()
        def state(step):
            if step.get('phase') == 'running' or step.get('slot_granted'):return '正在执行'
            recovered = step.get('last_success', 0) >= step.get('last_attempt', float('inf'))
            return '异常待处理' if not recovered and (step.get('failures') or step.get('blocked')) else '等待执行'
        def leaves(master):
            return [p for step in master.get('steps', []) for p in (step.get('range_steps') or [step])]
        def update(key, parent, text, expand=False):
            seen.add(key)
            item = self.items.get(key)
            if item is None:
                item = QTreeWidgetItem(parent if parent is not None else self.tree)
                self.items[key] = item
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator if expand else QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless)
            for i, value in enumerate(text):
                if item.text(i) != value:item.setText(i, value)
            return item
        grouped = {}
        for master in rows:grouped.setdefault(master['shop_id'], []).append(master)
        for sid, masters in grouped.items():
            steps = [s for m in masters for s in leaves(m)]
            running = sum(state(s) == '正在执行' for s in steps)
            issues = sum(state(s) == '异常待处理' for s in steps)
            statuses = [s.get('upload_timing', {}) for s in steps if s.get('upload_timing')]
            pending = sum(a.get('state') != 'confirmed' for a in statuses)
            dates = [s.get('last_timing', {}).get('finished_at') for s in steps if s.get('upload_timing', {}).get('state') in ('pending', 'failed', 'uploading')]
            from datetime import datetime
            oldest = min((v for v in dates if v), default=None)
            upload = ('本地已保存，等待上传 ' + str(pending) + '项' if pending else '中心已确认' if statuses else '尚无发布记录')
            if oldest:upload += ' · 最早 ' + datetime.fromtimestamp(oldest).strftime('%m-%d %H:%M:%S')
            shop = update('shop-' + str(sid), None, [str(masters[0].get('shop_label') or masters[0].get('shop_name') or f'店铺 {sid}'),
                f'正在执行 {running} · 等待执行 {len(steps)-running-issues} · 异常待处理 {issues}', '', '', '', '', '', upload], True)
            if not shop.isExpanded():continue
            for master in masters:
                key = str(sid) + ':' + master['master_id']
                parent = update(key, shop, [master['module'], master.get('stage', ''),
                    *[f"{master.get('timing_seconds', {}).get(k, 0):.1f}秒" for k in PHASES], '展开查看子步骤'], True)
                if not parent.isExpanded():continue
                for step in leaves(master):
                    timing = step.get('timing_seconds') or (step.get('last_timing') or {}).get('timing_seconds', {})
                    active = step.get('timing_current') or {}
                    stage = state(step) + ' · ' + step.get('stage', '') + (f" · {active.get('reason', '')} {active.get('elapsed_seconds', 0)}秒" if active else '')
                    if step.get('unblock_condition'):stage += ' · 解除条件：' + step['unblock_condition']
                    ack = step.get('upload_timing') or {}
                    confirmation = ('中心已确认 ' if ack.get('state') == 'confirmed' else '本地已保存，失败待重试 ' if ack.get('state') == 'failed' else '本地已保存，等待确认 ') + str(ack.get('seconds', 0)) + '秒' if ack else '尚无发布记录'
                    if ack.get('reason'):confirmation += ' · ' + ack['reason']
                    update(key + ':' + step['cache_key'], parent, [step.get('name', step['cache_key']), stage,
                        *[f'{timing.get(k, 0):.1f}秒' for k in PHASES], confirmation])
        for key in sorted(self.items, key=lambda k: k.count(':'), reverse=True):
            if key not in seen:
                item = self.items.pop(key)
                if item.parent() is not None:item.parent().removeChild(item)
                else:self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        d = self.dispatch
        detail = (' · 读取 ' + str(d.get('reading', 0)) + '/' + str(d['limits']['max_concurrent']) +
                  ' · 入库 ' + str(d.get('database',0)) + ' · 等待续读 ' + str(d.get('resuming',0)) +
                  ' · 浏览器准备 ' + str(d.get('preparing_shops',0))) if d.get('limits') else ''
        reasons = '；'.join(f"{d.get('reason_labels',{}).get(k,k)} {v}项" for k,v in d.get('waiting_reasons',{}).items())
        self.note.setText(f'每店到期执行 · {len(grouped)}家店铺 · {len(rows)}个展示分组{detail}。{reasons}；诊断日志仅保存文件。')
