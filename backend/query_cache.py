"""Small, process-local response cache; revisions fence writes racing a read."""

import time
from collections import OrderedDict


class QueryCache:
    def __init__(self, ttl=30, max_bytes=16 * 1024 * 1024, max_entries=256):
        self.ttl, self.max_bytes, self.max_entries = ttl, max_bytes, max_entries
        self.items = OrderedDict()
        self.size = 0

    def get(self, key, revision):
        item = self.items.get(key)
        if not item:
            return None
        expires, version, body, content_type = item
        if version != revision or expires <= time.monotonic():
            self.size -= len(self.items.pop(key)[2])
            return None
        self.items.move_to_end(key)
        return body, content_type

    def put(self, key, revision, body, content_type):
        if len(body) > self.max_bytes:
            return
        if key in self.items:
            self.size -= len(self.items.pop(key)[2])
        self.items[key] = (time.monotonic() + self.ttl, revision, body, content_type)
        self.size += len(body)
        while len(self.items) > self.max_entries or self.size > self.max_bytes:
            self.size -= len(self.items.popitem(last=False)[1][2])
