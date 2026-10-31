"""Parallel isolated readers, exclusive login/upload/control operations."""

import asyncio
from contextlib import asynccontextmanager


class ShopLock:
    def __init__(self):
        self.readers = 0
        self.writer = False
        self.waiting = 0
        self.changed = asyncio.Event()
        self.changed.set()

    def locked(self):
        return self.writer or self.readers > 0

    async def acquire(self):
        self.waiting += 1
        try:
            while self.locked():
                self.changed.clear()
                await self.changed.wait()
            self.writer = True
            return True
        finally:
            self.waiting -= 1
            self.changed.set()

    def release(self):
        if not self.writer:
            raise RuntimeError("Lock is not acquired")
        self.writer = False
        self.changed.set()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        self.release()

    async def read(self):
        while self.writer or self.waiting:
            self.changed.clear()
            await self.changed.wait()
        self.readers += 1
        return ReadLease(self)


class ReadLease:
    def __init__(self, lock):
        self.lock = lock
        self.active = True

    def release(self):
        if self.active:
            self.active = False
            self.lock.readers -= 1
            self.lock.changed.set()

    @asynccontextmanager
    async def exclusive(self):
        self.release()
        async with self.lock:
            yield
        # On failure/cancellation leave this lease released. The owning task exits.
        while self.lock.writer or self.lock.waiting:
            self.lock.changed.clear()
            await self.lock.changed.wait()
        self.lock.readers += 1
        self.active = True


class CollectionSession:
    """Task-local progress, shared identity and browser, isolated ReadPage targets."""

    def __init__(self, session):
        self.source = session
        self.read_progress = None
        self.collection_lease = None

    def __getattr__(self, name):
        return getattr(self.source, name)

    async def recover_home(self):
        if self.collection_lease:
            async with self.collection_lease.exclusive():
                return await self.source.recover_home()
        return await self.source.recover_home()
