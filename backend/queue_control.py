"""Cancel pending workers without touching active reads or durable checkpoints."""

import asyncio
import time
from . import collection


def held_until(service, key):
    holds = getattr(service, "queue_clear_holds", {})
    deadline = holds.get(key, 0)
    if deadline <= time.time():
        holds.pop(key, None)
        return 0
    return deadline


async def clear(service):
    now = time.time()
    config = service.queue_config
    holds = getattr(service, "queue_clear_holds", None)
    if holds is None:
        holds = service.queue_clear_holds = {}
    for key in list(holds):
        if holds[key] <= now:
            holds.pop(key, None)
    planned = getattr(getattr(service, 'due_plans', None), 'entries', {})
    cleared_plans = 0
    for key, (_, meta) in list(planned.items()):
        if key in service.jobs or meta.get('wait_code') == 'not_due' or meta.get('await_discovery'):
            continue
        if config.get('frequency_version'):
            from .frequency_policy import next_run
            deadline = next_run(meta['kind'], meta.get('scope', {}), config, meta.get('platform', 'pdd'), now, now) or now+60
        else:
            deadline = now + collection.interval_seconds(meta['kind'], config, meta.get('platform', 'pdd'), meta.get('scope', {}))
        holds[key] = deadline
        planned.pop(key, None)
        service.deferred.pop(key, None)
        cleared_plans += 1
    cancelled = []
    # No await between identifying queued tasks, setting holds and cancelling.
    # A granted worker cannot transition to running halfway through this loop.
    for key, meta in list(service.progress.items()):
        if meta.get("phase") != "queued" or meta.get("clearing_queue"):
            continue
        task = service.jobs.get(key)
        if task is None or task.done():
            continue
        if config.get("frequency_version"):
            from .frequency_policy import next_run

            deadline = (
                next_run(meta["kind"], meta.get("scope", {}), config, meta.get("platform", "pdd"), now, now) or now + 60
            )
        else:
            deadline = now + collection.interval_seconds(
                meta["kind"], config, meta.get("platform", "pdd"), meta.get("scope", {})
            )
        holds[key] = deadline
        meta["clearing_queue"] = True
        task.cancel()
        cancelled.append((key, meta, task))
    if cancelled:
        await asyncio.gather(*(task for _, _, task in cancelled), return_exceptions=True)
    for key, meta, task in cancelled:
        # Also remove jobs cancelled before their coroutine entered its finally.
        if service.jobs.get(key) is task:
            service.jobs.pop(key, None)
        if service.progress.get(key) is meta:
            service.progress.pop(key, None)
        service.deferred.pop(key, None)
    service.wake.set()
    service.schedule_wake.set()
    dispatcher = getattr(service,'due_plans',None)
    if dispatcher:
        # Wake preparation belongs to a plan, not a reading execution. Drain
        # preparations whose final eligible plan was cleared before returning.
        remaining = {key[0] for key in planned if dispatcher.pending(key)}
        tasks = [task for sid,task in dispatcher.preparations.items() if sid not in remaining]
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        dispatcher.request_dispatch()
    return {
        "ok": True,
        "count": len(cancelled) + cleared_plans,
        "running": sum(m.get("phase") == "running" for m in service.progress.values()),
    }
