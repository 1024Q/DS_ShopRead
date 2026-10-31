"""Small collector status projection, shared with the browser-free query process."""

import logging
import asyncio, json, time
from types import SimpleNamespace


def initialize(store):
    with store.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS collector_runtime(id INTEGER PRIMARY KEY,payload TEXT NOT NULL,updated REAL NOT NULL)"
        )


def capture(service, sessions):
    return {
        "sessions": {
            str(sid): {
                k: getattr(item, k, None) for k in ("state", "visible", "error", "port", "headless", "recovery_status")
            }
            for sid, item in sessions.items()
        },
        "progress": [[sid, key, dict(value)] for (sid, key), value in service.progress.items()],
        "deferred": [[sid, key, dict(value)] for (sid, key), value in service.deferred.items()],
        "waiting_login": [list(k) for k in service.waiting_login],
        "summary": {
            "running": sum(m.get("phase") == "running" for m in service.progress.values()),
            "queued": sum(m.get("phase") == "queued" for m in service.progress.values()),
            "deferred": len(service.deferred),
            "errors": len(service.errors),
            "waiting_login": len(service.waiting_login),
        },
        "collection": service.queue_config,
    }


def write(store, text):
    data = json.loads(text)
    with store.connect() as db:
        active = {(sid, key.split(":")[0]) for sid, key, _ in data.get("progress", [])}
        errors = {
            (r["shop_id"], r["kind"])
            for r in db.execute(
                "SELECT shop_id,kind FROM collection_tasks WHERE schedule_state<>'archived' AND last_error<>''"
            )
        }
        stopped = {int(sid) for sid, value in data.get("sessions", {}).items() if value.get("state") == "stopped"}
        data["summary"]["errors"] = sum(sid not in stopped for sid, kind in errors - active)
        db.execute(
            "INSERT OR REPLACE INTO collector_runtime(id,payload,updated) VALUES(1,?,?)",
            (json.dumps(data, ensure_ascii=False), time.time()),
        )


def save(store, service, sessions):
    write(store, json.dumps(capture(service, sessions), ensure_ascii=False))


async def run(store, service, sessions):
    while True:
        text = json.dumps(capture(service, sessions), ensure_ascii=False)
        try:
            await asyncio.to_thread(write, store, text)
        except Exception:
            store.log_event("warning", "system", "runtime_projection", "运行状态暂未写入，下轮重试")
        await asyncio.sleep(2)


def load(store, service, sessions):
    with store.connect() as db:
        row = db.execute("SELECT * FROM collector_runtime WHERE id=1").fetchone()
    fresh = bool(row and time.time() - row["updated"] < 20)
    data = json.loads(row["payload"]) if row else {}
    service.public_summary = {
        "summary": data.get("summary", {}),
        "collection": data.get("collection", service.queue_config),
        "collector_online": fresh,
    }
    if not fresh:
        service.public_summary["summary"] = {**service.public_summary["summary"], "running": 0, "queued": 0}
    service.public_sessions = data.get("sessions", {}) if fresh else {}
    service.progress = {(sid, key): value for sid, key, value in data.get("progress", [])} if fresh else {}
    service.jobs = {key: True for key in service.progress}
    service.deferred = {(sid, key): value for sid, key, value in data.get("deferred", [])} if fresh else {}
    service.waiting_login = {tuple(k) for k in data.get("waiting_login", [])} if fresh else set()
    for shop in store.list():
        info = data.get("sessions", {}).get(str(shop["id"]), {})
        sessions[shop["id"]] = SimpleNamespace(
            shop=shop,
            state=info.get("state", "stopped") if fresh else "stopped",
            recovery_status=info.get("recovery_status", "") if fresh else "采集服务离线，显示已存数据",
        )


async def follow(store, service, sessions):
    failures = 0
    while True:
        try:
            await asyncio.to_thread(load, store, service, sessions)
            failures = 0
        except Exception:
            failures += 1
            # Rate-limited: first failure and then about every 5 minutes.
            if failures == 1 or failures % 150 == 0:
                logging.getLogger(__name__).warning("运行状态同步失败（连续 %d 次）", failures, exc_info=True)
        await asyncio.sleep(2)
