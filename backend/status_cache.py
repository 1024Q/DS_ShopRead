"""Per-shop task summary cache for status polling.

The header/overview poll used to aggregate every shop database on every
refresh. Each shop's aggregate is now kept in memory and rebuilt only after a
write touched that shop (cache publication, archive, task claim/finish/yield,
registration) or after a short safety TTL for writers that do not report.
"""

import threading
import time
from contextlib import contextmanager

TTL = 20.0
_lock = threading.Lock()
_generations = {}  # (directory, shop_id) -> int
_entries = {}  # (directory, shop_id) -> (generation, stamp, value)


@contextmanager
def invalidating(store, shop_id):
    """Wrap outside a DB context so invalidation runs after commit/rollback."""
    try:
        yield
    finally:
        touch(store, shop_id)


def _key(store, shop_id):
    return (str(getattr(store, "directory", "")), int(shop_id))


def touch(store, shop_id):
    """Mark one shop's summary stale; cheap and safe to call from any writer."""
    try:
        key = _key(store, shop_id)
    except TypeError, ValueError:
        return
    with _lock:
        _generations[key] = _generations.get(key, 0) + 1
        _entries.pop(key, None)


def touch_all(store):
    directory = str(getattr(store, "directory", ""))
    with _lock:
        for key in [k for k in (_generations.keys() | _entries.keys()) if k[0] == directory]:
            _generations[key] = _generations.get(key, 0) + 1
            _entries.pop(key, None)


def get(store, shop_id, load, now=None):
    """Return the shop's cached value, calling load() only when it is stale."""
    now = time.monotonic() if now is None else now
    key = _key(store, shop_id)
    with _lock:
        generation = _generations.setdefault(key, 0)
        entry = _entries.get(key)
        if entry and entry[0] == generation and now - entry[1] < TTL:
            return entry[2]
    value = load()
    with _lock:
        # A write that landed while loading keeps the entry stale.
        if _generations.get(key, 0) == generation:
            _entries[key] = (generation, now, value)
    return value


def forget(store, shop_id):
    key = _key(store, shop_id)
    with _lock:
        _entries.pop(key, None)
        _generations[key] = _generations.get(key, 0) + 1
