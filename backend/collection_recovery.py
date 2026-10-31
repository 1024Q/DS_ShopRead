"""Repair abandoned scheduling state without forcing retries or erasing data."""

import time
from .async_database import run as database_work


def release_orphans(store, shop_id, active, now):
    with store.connect() as db:
        keys = [
            r["cache_key"]
            for r in db.execute(
                "SELECT cache_key FROM collection_tasks WHERE shop_id=? AND lease_until>0 AND last_attempt<?",
                (shop_id, now - 30),
            )
            if r["cache_key"] not in active
        ]
    if not keys:
        return 0
    repaired = 0
    with store.write_connection() as db:
        for key in keys:
            repaired += db.execute(
                "UPDATE collection_tasks SET lease_until=0 WHERE shop_id=? AND cache_key=? AND lease_until>0 AND last_attempt<?",
                (shop_id, key, now - 30),
            ).rowcount
    return repaired


async def watchdog(service, now=None):
    now = time.time() if now is None else now
    # Follow the live reading deadline, including pauses for durable DB work.
    # A write/final publication is not a hung browser read and must finish first.
    for key, meta in list(service.progress.items()):
        task = service.jobs.get(key)
        if not task or task.done():
            service.progress.pop(key, None)
            service.jobs.pop(key, None)
            service.wake.set()
        elif (
            meta.get("phase") == "running"
            and not meta.get("timeout_paused")
            and not meta.get("read_finished")
            and now > meta.get("read_deadline", meta.get("started_at", now) + meta.get("timeout_seconds", 600)) + 60
            and not task.cancelling()
        ):
            task.cancel()
            await database_work(
                service.store.log_event,
                "warning",
                meta["kind"],
                "stalled_collection_recovered",
                "采集超过配置超时及清理宽限期，自动取消失效任务；保留已保存分页，释放后重新调度",
                key[0],
            )


def browser_restored(store, shop_id, checked_at):
    """Release only pre-recovery connection/login failures, not rate limits or live work."""
    with store.write_connection() as db:
        return db.execute(
            """UPDATE collection_tasks SET retry_at=0,blocked=0
            WHERE shop_id=? AND error_kind IN ('network','login') AND last_attempt<=?
              AND lease_until=0 AND (retry_at>0 OR blocked=1)""",
            (shop_id, checked_at),
        ).rowcount
