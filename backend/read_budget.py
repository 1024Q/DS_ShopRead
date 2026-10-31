"""Reading time excludes durable database work and ends before final publication."""

import asyncio
import contextvars
import time
from contextlib import contextmanager

current = contextvars.ContextVar("read_budget", default=None)


class ReadBudget:
    def __init__(self, seconds, meta):
        self.seconds = seconds
        self.meta = meta
        self.done = False
        self.depth = 0

    async def __aenter__(self):
        self.started = time.monotonic()
        self.timer = asyncio.timeout(self.seconds)
        await self.timer.__aenter__()
        self.token = current.set(self)
        self.meta["read_deadline"] = time.time() + self.seconds
        return self

    async def __aexit__(self, *args):
        current.reset(self.token)
        return await self.timer.__aexit__(*args)

    def remaining(self):
        deadline = self.timer.when()
        return max(0, deadline - asyncio.get_running_loop().time()) if deadline is not None else self.saved

    @contextmanager
    def database(self):
        with self.pause("database_wait_seconds"):
            yield

    @contextmanager
    def operation(self):
        with self.pause("priority_operation_seconds"):
            yield

    @contextmanager
    def pause(self, field):
        if self.done or self.depth or self.timer.expired():
            yield
            return
        self.saved = self.remaining()
        self.depth += 1
        self.timer.reschedule(None)
        self.meta.update(timeout_paused=True, read_remaining_seconds=self.saved)
        waiting = time.monotonic()
        try:
            yield
        finally:
            self.meta[field] = self.meta.get(field, 0) + time.monotonic() - waiting
            self.depth -= 1
            if not self.done and not self.timer.expired():
                self.timer.reschedule(asyncio.get_running_loop().time() + self.saved)
                self.meta.update(timeout_paused=False, read_deadline=time.time() + self.saved)

    def finish(self):
        self.done = True
        self.timer.reschedule(None)
        self.meta["read_seconds"] = max(0, time.monotonic() - self.started - self.meta.get("database_wait_seconds", 0)
                                        - self.meta.get("priority_operation_seconds", 0)
                                        - self.meta.get("slot_wait_seconds", 0)
                                        - self.meta.get("page_load_seconds", 0))
        self.meta.update(read_finished=True, timeout_paused=False)


def remaining(meta, now):
    if not meta.get("started_at") or meta.get("read_finished") or meta.get("timeout_paused"):
        return None
    return max(0, round(meta.get("read_deadline", meta["started_at"] + meta.get("timeout_seconds", 240)) - now))


def finish():
    budget = current.get()
    if budget:
        budget.finish()
