"""Resolve log identity from the node management DB, with a bounded last-good cache."""
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from contextlib import closing

_lock = threading.Lock()
_cache = OrderedDict()


def identity(path, shop_id):
    if not shop_id:
        return '', ''
    key = (str(Path(path).resolve()), int(shop_id))
    try:
        # A read-only connection must never create an accidental empty database.
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=0)) as db:
            row = db.execute("SELECT coalesce(nullif(alias,''),nullif(name,''),username),platform FROM shops WHERE id=?", (shop_id,)).fetchone()
        if row and row[0]:
            with _lock:
                _cache[key] = tuple(row)
                _cache.move_to_end(key)
                if len(_cache) > 2048:
                    _cache.popitem(last=False)
            return tuple(row)
    except sqlite3.Error:
        pass
    with _lock:
        return _cache.get(key, ('', ''))
