"""Bounded file-only collection steps, separate from final business outcomes."""

from .collector_logs import DailyLog
import threading

_lock = threading.Lock()


def step(store, shop, kind, stage):
    if not stage:
        return
    name = shop.get("name") or shop.get("detected_name") or shop.get("username") or str(shop.get("id", ""))
    try:
        with _lock:
            DailyLog(store.directory / "launcher-logs", "operations").write(f"INFO [{kind}] {name} {stage}")
    except OSError:
        pass


def outcome(store, job):
    level = "SUCCESS" if job["state"] == "completed" else "ERROR"
    try:
        with _lock:
            DailyLog(store.directory / "launcher-logs", "results").write(
                f"{level} [报表导出] {job['shop_name']} · {job['start']} 至 {job['end']} · {job['message']}"
            )
    except OSError:
        pass
