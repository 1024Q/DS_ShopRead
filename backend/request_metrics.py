import threading
from collections import deque

_lock = threading.Lock()
_rows = {}


def record(path, elapsed, cache):
    with _lock:
        if path not in _rows and len(_rows) >= 100:
            _rows.pop(next(iter(_rows)))
        row = _rows.setdefault(path, {"samples": deque(maxlen=100), "count": 0, "hits": 0})
        row["samples"].append(round(elapsed, 1))
        row["count"] += 1
        row["hits"] += cache == "hit"


def snapshot():
    with _lock:
        result = []
        for path, row in _rows.items():
            samples = sorted(row["samples"])
            result.append(
                {
                    "path": path,
                    "count": row["count"],
                    "hits": row["hits"],
                    "last": row["samples"][-1],
                    "p95": samples[min(len(samples) - 1, int(len(samples) * 0.95))],
                }
            )
        return {"rows": result}
