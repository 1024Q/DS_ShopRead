"""Bounded collection diagnostics: memory and files only, never business tables."""
import contextvars
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from functools import wraps

current = contextvars.ContextVar('collection_metrics', default=None)
PHASES = {'slot_wait': '等待执行名额', 'browser_start': '浏览器启动', 'page_load': '页面加载',
          'query': '接口查询', 'database': '数据库入库', 'upload': '上传确认',
          'request_wait': '等待同店请求间隔', 'shared_wait': '等待相同查询结果', 'local_read': '本地查询'}
_lock = threading.RLock()
_recent = {}
_disk = {}
_disk_stamps = {}
_deliveries = {}


def measured(name):
    def decorate(fn):
        @wraps(fn)
        async def run(*args, **kwargs):
            with phase(name):
                from .read_budget import current as read_budget
                budget = read_budget.get()
                if name == 'page_load' and budget and not budget.done:
                    import asyncio
                    from .read_errors import PageReadError
                    # Navigation has its own bounded deadline. Time spent here
                    # never consumes the configured interface-reading budget.
                    with budget.pause('page_load_seconds'):
                        try:
                            async with asyncio.timeout(max(10,min(90,budget.seconds))):
                                return await fn(*args,**kwargs)
                        except TimeoutError as error:
                            raise PageReadError('页面加载超时，保留已提交断点；仅核验当前页面资源') from error
                return await fn(*args, **kwargs)
        return run
    return decorate


@contextmanager
def phase(name, reason=''):
    tracker = current.get()
    if tracker is None:
        yield
        return
    meta = tracker.meta
    previous = meta.get('timing_current')
    started = time.monotonic()
    durations = meta.setdefault('timing_seconds', {})
    if previous:
        key = previous['phase']
        durations[key] = durations.get(key, 0) + max(0, started - previous['_tick'])
    frame = {'phase': name, 'since': time.time(), 'reason': reason or PHASES.get(name, name), '_tick': started}
    meta['timing_current'] = frame
    try:
        yield
    finally:
        durations[name] = durations.get(name, 0) + max(0, time.monotonic() - frame['_tick'])
        if previous:
            previous['_tick'], previous['since'] = time.monotonic(), time.time()
        meta['timing_current'] = previous


def count(name, amount=1):
    tracker = current.get()
    if tracker:
        values = tracker.meta.setdefault('reuse_counts', {})
        values[name] = values.get(name, 0) + amount


def fields(meta, now=None):
    now = time.time() if now is None else now
    seconds = dict(meta.get('timing_seconds', {}))
    active = meta.get('timing_current') or {}
    elapsed = max(0, now - active.get('since', now))
    if active.get('phase'):
        seconds[active['phase']] = seconds.get(active['phase'], 0) + elapsed
    return {'timing_seconds': {k: round(v, 3) for k, v in seconds.items()},
            'timing_current': {k:v for k,v in {**active, 'elapsed_seconds': round(elapsed, 1)}.items() if not k.startswith('_')} if active else None,
            'reuse_counts': dict(meta.get('reuse_counts', {}))}


class Tracker:
    def __init__(self, store, shop, cache_key, kind, scope, config, meta):
        from .master_tasks import identifier
        from .task_catalog import title
        self.store, self.meta = store, meta
        self.identity = {'shop_id': shop['id'], 'shop_name': shop.get('name', ''), 'cache_key': cache_key,
                         'kind': kind, 'step': title(kind, scope), 'master_id': identifier(kind, scope, config, shop.get('platform', 'pdd')),
                         'started_at': time.time()}
        self.token = current.set(self)

    def finish(self, result):
        current.reset(self.token)
        if self.store is None:
            return
        row = {**self.identity, **fields(self.meta), 'result': result, 'finished_at': time.time(),
               'delivery_scope': self.meta.get('delivery_scope'),
               'wait_reason_seconds': dict(self.meta.get('wait_reason_seconds', {})),
               'stage_durations': dict(self.meta.get('stage_durations', {}))}
        row['timing_current'] = None
        directory = Path(self.store.directory)
        try:
            from .collector_logs import DailyLog
            from .collector_update import write_json
            with _lock:
                key = str(directory)
                recent = _recent.setdefault(key, {})
                recent.pop((row['shop_id'], row['cache_key']), None)
                recent[row['shop_id'], row['cache_key']] = row
                while len(recent) > 500:
                    recent.pop(next(iter(recent)))
                DailyLog(directory / 'launcher-logs', 'performance').write(json.dumps(row, ensure_ascii=False))
                write_json(directory / 'collection-performance.json', {'updated': time.time(), 'rows': list(recent.values())})
        except OSError:
            # Telemetry failure cannot fail publication or erase a checkpoint.
            pass


def file_rows(path):
    with _lock:
        stamp, rows = _disk.get(str(path), (0, []))
        if time.monotonic() - stamp > 2:
            from .collector_update import read_json
            try:
                info = path.stat()
                fingerprint = (info.st_mtime_ns, info.st_size)
                if _disk_stamps.get(str(path)) != fingerprint:
                    rows = read_json(path, {}).get('rows', [])
                    _disk_stamps[str(path)] = fingerprint
            except (OSError, ValueError):
                pass
            _disk[str(path)] = (time.monotonic(), rows)
        return rows


def recent(directory):
    return {(r['shop_id'], r['cache_key']): r for r in file_rows(Path(directory) / 'collection-performance.json')}


def delivery(directory, lane, rows, elapsed, state='confirmed', reason=''):
    """Upload diagnostics never substitute for a durable acknowledgement."""
    from .collector_logs import DailyLog
    try:
        with _lock:
            directory = Path(directory)
            root = directory.parent.parent if directory.parent.name == 'shops' else directory
            log = DailyLog(root / 'launcher-logs', 'performance-upload')
            key = (str(root), str(directory.name), lane)
            values = _deliveries.setdefault(key, {})
            for row in rows:
                value = {'shop_id': row['shop'], 'scope': row['scope'], 'source_at': row['updated'],
                    'confirmed_at': time.time() if state == 'confirmed' else None, 'upload_seconds': max(0, time.time() - row['updated']),
                    'http_seconds': elapsed, 'lane': lane, 'state': state, 'reason': reason}
                log.write(json.dumps(value, ensure_ascii=False))
                values.pop(row['scope'], None); values[row['scope']] = value
            while len(values) > 200: values.pop(next(iter(values)))
            from .collector_update import write_json
            write_json(root / f'performance-upload-{directory.name}-{lane}.json', {'rows': list(values.values())})
    except (OSError, KeyError, TypeError, ValueError):
        pass


def enrich(service, sid, key, meta, history):
    """Attach current/last measurements; never read business payloads."""
    live = fields(meta)
    last = history.get((sid, key))
    result = {**live, 'last_timing': last}
    if not last or last.get('result') not in ('success', 'partial') or not last.get('delivery_scope'):
        return result
    directory = Path(service.store.directory)
    lane = 'auxiliary' if last['delivery_scope'].startswith('attention:') else 'business'
    path = directory / f'performance-upload-{sid}-{lane}.json'
    rows = file_rows(path)
    ack = next((r for r in rows if r['scope'] == last['delivery_scope'] and r['source_at'] >= last['started_at']), None)
    result['upload_timing'] = ({'state': ack.get('state', 'confirmed'), 'seconds': round(ack['upload_seconds'], 3), 'confirmed_at': ack['confirmed_at'], 'reason': ack.get('reason', '')}
        if ack else {'state': 'pending', 'seconds': round(max(0, time.time() - last['finished_at']), 1), 'reason': '本地已入库，等待中心确认'})
    return result
