"""Wake local SSE readers after commit; periodic database reads cover other workers."""

import asyncio
import threading


class ChangeSignal:
    def __init__(self):
        self.lock = threading.Lock()
        self.version = 0
        self.nodes = {}
        self.listeners = set()

    def notify(self, node, records):
        if not any(r["scope"] != "collector_status" and not r["scope"].startswith("response:") for r in records):
            return
        with self.lock:
            self.version += 1
            self.nodes[node] = self.version
            listeners = list(self.listeners)
        for loop, event in listeners:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass  # A disconnected SSE client must never fail a committed upload.

    def revision(self, node=None):
        with self.lock:
            return self.nodes.get(node, 0) if node is not None else self.version

    async def wait(self, previous, timeout=5):
        entry = asyncio.get_running_loop(), asyncio.Event()
        with self.lock:
            self.listeners.add(entry)
            changed = previous != self.version
        try:
            if not changed:
                try:
                    await asyncio.wait_for(entry[1].wait(), timeout)
                except TimeoutError:
                    pass
        finally:
            with self.lock:
                self.listeners.discard(entry)
