"""Explicit platform-processing exception; normal cadence/fairness is unchanged."""

import json
import time
import logging
from pathlib import Path
from .priority_rules import immediate

_cache = {}
_receipts = {}
POLICY_VERSION = 2  # Invalidate old attempt-only receipts, never financial intents.


def expedited(meta):
    return (meta.get("urgent_refund") is True and meta.get("kind") == "aftersales"
            and bool(meta.get("scope", {}).get("_attention")) and meta.get("platform", "pdd") == "pdd")


def demand(kind, scope, state, platform="pdd"):
    return expedited({**state, "kind": kind, "scope": scope, "platform": platform})


def receipt_path(store, shop_id):
    return Path(store.directory) / 'refund-priority' / (str(int(shop_id)) + '.json')


def needs_run(store, shop_id, candidates):
    return bool(ordered(store, shop_id, candidates))


def source(row):
    return {key: row.get(key) for key in ('order_number', 'status', 'progress', 'amount', 'deadline')}


def receipt(store, shop_id):
    from .collector_update import read_json

    saved = read_json(receipt_path(store, shop_id), {})
    saved = saved if isinstance(saved, dict) and saved.get('policy_version') == POLICY_VERSION else {}
    memory = _receipts.get((str(store.directory), int(shop_id)), {})
    return memory if memory.get('updated', 0) > saved.get('updated', 0) else saved


def ordered(store, shop_id, candidates):
    results = receipt(store, shop_id).get('results', {})
    now = time.time()
    due = [row for row in candidates if (result := results.get(str(row['id']), {})).get('source') != source(row)
           or result.get('retry_at', 0) <= now]
    return sorted(due, key=lambda row: (results.get(str(row['id']), {}).get('checked_at', 0),
                  str(row.get('deadline') or '9999'), str(row['id'])))


def checked(store, shop_id, candidates, attempted, outcomes=None):
    old = receipt(store, shop_id)
    outcomes = outcomes or {}
    results = {}
    for row in candidates:
        case = str(row['id'])
        result = old.get('results', {}).get(case)
        if case in outcomes:
            result = {**outcomes[case], 'source': source(row), 'order_number': row.get('order_number')}
        elif case in attempted:
            result = {'source': source(row), 'order_number': row.get('order_number'),
                      'state': 'checked', 'reason': '核验完成，等待后续复核',
                      'checked_at': time.time(), 'retry_at': time.time() + 300}
        if result:
            results[case] = result
    save_results(store, shop_id, results)


def deferred(store, shop_id, candidates, reason, seconds=30):
    """Record an unsubmitted resource wait without claiming a refund."""
    results = dict(receipt(store, shop_id).get('results', {}))
    now = time.time()
    for row in candidates:
        results[str(row['id'])] = {
            'source': source(row), 'order_number': row.get('order_number'),
            'state': 'retry', 'reason': reason, 'checked_at': now,
            'retry_at': now + max(1, seconds),
        }
    save_results(store, shop_id, results)


def save_results(store, shop_id, results):
    from .collector_update import write_json

    path = receipt_path(store, shop_id)
    key = (str(store.directory), int(shop_id))
    values = {case: value.get('order_number') for case, value in results.items() if value.get('state') != 'retry'}
    saved = {'policy_version': POLICY_VERSION, 'checked': values, 'results': results, 'updated': time.time()}
    _receipts[key] = saved
    if len(_receipts) > 1024:
        _receipts.pop(next(iter(_receipts)))
    try:
        write_json(path, saved)
    except OSError:
        logging.getLogger(__name__).warning('插队调度回执保存延后；本进程已合并需求，财务提交记录不受影响 · shop=%s', shop_id)


def pending(store, shop_id):
    """Read the durable attention index; intent files still block every replay."""
    from .auto_refunds import record_path

    folder = Path(store.directory) / "refund-actions" / str(shop_id)
    try:
        stamp = folder.stat().st_mtime_ns
    except OSError:
        stamp = 0
    key = (str(store.directory), int(shop_id))
    with store.shop_db(shop_id) as db:
        saved = db.execute("SELECT updated FROM attention_snapshots WHERE shop_id=? AND kind='aftersales'", (shop_id,)).fetchone()
        revision = saved[0] if saved else 0
        old = _cache.get(key)
        if old and old[:2] == (revision, stamp) and time.monotonic() - old[2] < 10:
            return list(old[3])
        rows = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM attention_rows WHERE shop_id=? AND kind='aftersales' "
            "AND json_extract(payload,'$.status')='平台处理中' ORDER BY coalesce(deadline,'9999'),id", (shop_id,))]
    result = []
    for row in rows:
        if not immediate('aftersales', row, 'pdd') or not str(row.get('id', '')).isdigit():
            continue
        if not record_path(store, shop_id, row['id']).exists():
            result.append(row)
    _cache[key] = (revision, stamp, time.monotonic(), result)
    if len(_cache) > 1024:
        _cache.pop(next(iter(_cache)))
    return list(result)
