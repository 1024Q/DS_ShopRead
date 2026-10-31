"""Persist pause before cancellation; cancelled readers flush checkpoints normally."""

import asyncio
from .async_database import run
from . import collection
from .shop_rotation import is_home


async def change(service, paused):
    config = await run(collection.save_settings, service.store, {"settings_patch": {"home_paused": paused}})
    tasks = [
        (k, t)
        for k, t in list(service.jobs.items())
        if is_home(
            service.progress.get(k, {}).get("kind", k[1].split(":")[0]), service.progress.get(k, {}).get("scope", {})
        )
    ]
    service.configure_queue(config)
    if paused:
        if getattr(service, 'direct_refunds', None):
            await service.direct_refunds.cancel_all()
        for _, task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*(t for _, t in tasks), return_exceptions=True)
        for key, task in tasks:
            if service.jobs.get(key) is task:
                service.jobs.pop(key, None)
                service.progress.pop(key, None)
    service.wake.set()
    service.schedule_wake.set()
    return {"paused": paused, "confirmed": True, "cancelled": len(tasks) if paused else 0}
