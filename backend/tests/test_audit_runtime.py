import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.async_database import run
from backend.center.replication_worker import Delivery, ReceiptCache
from backend.center.shop_delivery import ShopDeliveryGroup
from backend.database_maintenance import database_capacity


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_cancellation_keeps_lock_until_native_write_finishes(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        lock = asyncio.Lock()

        def write():
            started.set()
            release.wait(3)
            finished.set()

        async def collect():
            async with lock:
                await run(write)

        task = asyncio.create_task(collect())
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(started.is_set())
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                self.assertTrue(lock.locked())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(finished.is_set())
            self.assertFalse(lock.locked())
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_shop_does_not_block_other_shops_or_backlog(self):
        with tempfile.TemporaryDirectory() as folder:
            group = ShopDeliveryGroup(folder, "orders", ["center", "node"])
            group.upload_slots = 2
            group.children = {sid: sid for sid in ("slow", "fast", "third")}
            release = asyncio.Event()
            fast_twice = asyncio.Event()
            running = set()
            counts = {}
            maximum = 0

            async def tick(child, client):
                nonlocal maximum
                self.assertNotIn(child, running)
                running.add(child)
                maximum = max(maximum, len(running))
                counts[child] = counts.get(child, 0) + 1
                try:
                    if child == "slow":
                        await release.wait()
                    if child == "fast" and counts[child] >= 2:
                        fast_twice.set()
                    return child == "fast"
                finally:
                    running.remove(child)

            with (
                patch.object(group, "discover"),
                patch.object(group, "aggregate"),
                patch.object(group, "tick", side_effect=tick),
            ):
                task = asyncio.create_task(group.run(None))
                try:
                    await asyncio.wait_for(fast_twice.wait(), 1.5)
                    self.assertIn("slow", running)
                    self.assertGreaterEqual(counts.get("third", 0), 1)
                    self.assertLessEqual(maximum, 2)
                finally:
                    task.cancel()
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertFalse(running)

    async def test_receipts_shared_across_shops_and_lanes_without_losing_newer_ack(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cache = ReceiptCache()
            requests = []
            children = []
            for sid, lane in (("1", "business"), ("2", "business"), ("1", "auxiliary")):
                directory = root / sid
                directory.mkdir(exist_ok=True)
                (directory / "business.sqlite3").touch()
                children.append(Delivery(directory, lane, ["center", "node"]))

            def handler(request):
                requests.append(request.url.path)
                return httpx.Response(
                    200,
                    json={
                        "records": [
                            {"shop": "1", "scope": "a", "updated": 1},
                            {"shop": "2", "scope": "a", "updated": 2},
                        ]
                    },
                )

            async with httpx.AsyncClient(base_url="http://center", transport=httpx.MockTransport(handler)) as client:
                snapshots = await asyncio.gather(*(cache.get(client) for _ in children))
                for child, snapshot in zip(children, snapshots):
                    child.apply_receipts(snapshot[2], snapshot[1])
                self.assertEqual(requests, ["/agent/receipts"])
                self.assertEqual(children[0].acks, {children[0].key("1", "a"): 1})
                self.assertEqual(children[1].acks, {children[1].key("2", "a"): 2})
                child = children[0]
                key = child.key("1", "new")
                child.acks[key] = 3
                child.confirmed[key] = snapshots[0][1] + 1
                child.apply_receipts(snapshots[0][2], snapshots[0][1])
                self.assertEqual(child.acks[key], 3)
                cache.next_check = 0
                fresh = await cache.get(client)
                child.confirmed[key] = fresh[1] - 1
                child.apply_receipts(fresh[2], fresh[1])
                self.assertNotIn(key, child.acks)  # A later center restore still resends lost records.
                self.assertEqual(len(requests), 2)

    async def test_receipt_failure_backs_off_without_blocking_saved_uploads(self):
        cache = ReceiptCache()
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(503)

        async with httpx.AsyncClient(base_url="http://center", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await cache.get(client)
            self.assertIsNone(await cache.get(client))
            self.assertEqual(len(requests), 1)


class CapacityTests(unittest.TestCase):
    def test_active_shards_and_sidecars_are_counted_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = {
                "shops.sqlite3": 100,
                "shops.sqlite3-wal": 7,
                "shops.sqlite3-shm": 3,
                "shops.sqlite3.bak": 999,
                "shops/1/business.sqlite3": 200,
                "shops/1/business.sqlite3-wal": 11,
                "shops/2/business.sqlite3": 300,
                "shops/2/business.sqlite3-shm": 13,
                "shops/3/business.sqlite3": 999,
            }
            for name, size in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x" * size)
            manifest = {
                "shops": {
                    "1": {"state": "active", "path": "shops/1/business.sqlite3"},
                    "2": {"state": "active", "path": "shops/2/business.sqlite3"},
                    "3": {"state": "inactive", "path": "shops/3/business.sqlite3"},
                    "duplicate": {"state": "active", "path": "shops/1/business.sqlite3"},
                }
            }
            (root / "shop-storage.json").write_text(json.dumps(manifest))
            capacity = database_capacity(root)
            self.assertEqual(
                capacity,
                {
                    "database_bytes": 634,
                    "management_bytes": 110,
                    "business_bytes": 524,
                    "wal_bytes": 18,
                    "shop_database_count": 2,
                },
            )
