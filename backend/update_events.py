"""Committed changes shared across collector and query processes."""

import asyncio, json, time, weakref
from .data_versions import changes

_readers = weakref.WeakKeyDictionary()


async def shared_changes(store, after):
    # Concurrent tabs at the same revision share one database read.
    cache = _readers.setdefault(store, {})
    now = time.monotonic()
    for key, (stamp, task) in list(cache.items()):
        if task.done() and now - stamp > 2:
            cache.pop(key, None)
    entry = cache.get(after)
    if entry is None:
        entry = (now, asyncio.create_task(asyncio.to_thread(changes, store, after)))
        cache[after] = entry
    try:
        return await asyncio.shield(entry[1])
    except Exception:
        cache.pop(after, None)
        raise


async def events(store, request, duration=50, interval=2):
    try:
        previous = int(getattr(request, "headers", {}).get("last-event-id", ""))
    except ValueError, TypeError:
        previous = None
    if previous is not None and previous < 0:
        previous = None
    started = time.monotonic()
    while time.monotonic() - started < duration:
        if await request.is_disconnected():
            return
        data = await shared_changes(store, previous)
        if data["version"] != previous:
            previous = data["version"]
            yield "id: " + str(previous) + "\nevent: database\ndata: " + json.dumps(data) + "\n\n"
        else:
            yield ": heartbeat\n\n"
        await asyncio.sleep(interval)
