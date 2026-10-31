"""Measure stage and current blocker separately from total queue residence."""

import time
from .collection_metrics import fields as metrics_fields


class TaskMeta(dict):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.update(dict(*args, **kwargs))

    def __setitem__(self, key, value):
        if key == "stage" and value != self.get("stage"):
            now = time.time()
            old = self.get("stage")
            started = self.get("stage_started_at")
            if old and started:
                durations = self.setdefault("stage_durations", {})
                label = old if old in durations or len(durations) < 32 else "其他阶段"
                durations[label] = durations.get(label, 0) + max(0, now - started)
            super().__setitem__("stage_started_at", now)
            callback = getattr(self, "on_stage", None)
            if callback:
                callback(value)
        super().__setitem__(key, value)

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value


def observe_wait(key, meta, progress, config, now=None):
    if meta.get("phase") != "queued":
        return
    from .collection_queue import waiting_reason, blocking_task

    now = time.time() if now is None else now
    blocker = blocking_task(key, meta, progress, now)
    reason = waiting_reason(key, meta, progress, config)
    identity = (
        ("module", blocker.get("kind"), blocker.get("started_at"))
        if blocker
        else (reason, meta.get("browser_wait_stage"))
    )
    if meta.get("wait_identity") != identity:
        end_wait(meta, now)
        meta.update(wait_identity=identity, wait_reason_since=now, wait_reason=reason)


def end_wait(meta, now=None):
    now = time.time() if now is None else now
    if meta.get('wait_reason_since') and meta.get('wait_reason'):
        waits = meta.setdefault('wait_reason_seconds', {})
        reason = meta['wait_reason']
        key = reason if reason in waits or len(waits) < 24 else '其他等待'
        waits[key] = waits.get(key, 0) + max(0, now - meta['wait_reason_since'])
        meta.pop('wait_reason_since', None)


def fields(meta, now=None):
    now = time.time() if now is None else now
    stage = max(0, int(now - meta["stage_started_at"])) if meta.get("stage_started_at") else None
    wait = (
        max(0, int(now - meta["wait_reason_since"]))
        if meta.get("phase") in ("queued", "waiting", "deferred") and meta.get("wait_reason_since")
        else None
    )
    return {
        **metrics_fields(meta, now),
        "stage_elapsed_seconds": stage,
        "current_wait_seconds": wait,
        "stage_durations": {k: round(v, 1) for k, v in meta.get("stage_durations", {}).items()},
        "database_wait_seconds": round(meta.get("database_wait_seconds", 0), 1),
        "read_seconds": round(meta.get("read_seconds", 0), 1),
    }


def text(meta):
    value = fields(meta)
    if meta.get("phase") == "queued" and value["current_wait_seconds"] is not None:
        return f"当前原因已持续 {value['current_wait_seconds']} 秒"
    if meta.get("phase") == "running" and value["stage_elapsed_seconds"] is not None:
        return f"当前阶段 {value['stage_elapsed_seconds']} 秒"
    return ""
