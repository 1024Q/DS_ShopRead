import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
import httpx
from backend.center.replication_worker import Delivery
from backend.center.snapshot_freshness import overlay
from backend.center.database import Database
from backend.center.business import load


class DeliveryOutboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "shops.sqlite3"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE business_cache(shop_id INTEGER,cache_key TEXT,payload TEXT,updated REAL,PRIMARY KEY(shop_id,cache_key))"
            )
        self.worker = Delivery(self.root, "business", ["center", "node"])

    def tearDown(self):
        self.temp.cleanup()

    def write(self, stamp, value=1):
        payload = {
            "rows": [{"id": "a", "value": value, "updated_at": "2026-10-29"}],
            "_sync": {"elapsed": stamp, "snapshot_started_at": stamp},
        }
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO business_cache VALUES(1,?,?,?)", ("orders:test", json.dumps(payload), stamp)
            )

    async def send(self, status=200):
        sent = []

        def receive(request):
            sent.append(json.loads(request.content)["records"])
            return httpx.Response(status)

        async with httpx.AsyncClient(base_url="http://center", transport=httpx.MockTransport(receive)) as client:
            await self.worker.send(client, self.worker.collect(now=10**12))
        return sent

    async def test_same_business_content_is_heartbeat_across_restart(self):
        self.write(100)
        first = await self.send()
        self.assertEqual(first[0][0]["scope"], "orders:test")
        self.worker = Delivery(self.root, "business", ["center", "node"])
        self.write(101)
        second = await self.send()
        self.assertEqual(second[0][0]["scope"], "freshness:orders:test")
        self.assertEqual(second[0][0]["payload"]["content_updated"], 100)
        self.assertEqual(second[0][0]["payload"]["_sync"]["snapshot_started_at"], 101)
        self.write(102, value=2)
        self.assertEqual((await self.send())[0][0]["scope"], "orders:test")

    async def test_three_permanent_rejections_quarantine_until_new_version(self):
        self.write(10)
        for _ in range(3):
            await self.send(422)
        self.assertFalse(self.worker.failures)
        self.assertEqual(self.worker.snapshot()["rejected_count"], 1)
        self.assertEqual(self.worker.collect(now=10**12), [])
        self.worker = Delivery(self.root, "business", ["center", "node"])
        self.assertEqual(self.worker.collect(now=10**12), [])
        self.write(11)
        self.assertEqual(len(self.worker.collect()), 1)
        await self.send()
        self.assertEqual(self.worker.snapshot()["rejected_count"], 0)

    async def test_idle_rounds_do_not_rewrite_state_and_ack_file_is_small(self):
        self.write(10)
        await self.send()
        value = json.loads(self.worker.file.read_text())
        self.assertTrue(value["outbox"])
        self.assertNotIn("acks", value)
        with patch("backend.center.replication_worker.save_state") as save:
            for _ in range(5):
                self.assertEqual(self.worker.collect(), [])
                self.worker.save()
            save.assert_not_called()

    async def test_oldest_backlog_survives_new_source_versions_and_restart_then_clears_on_ack(self):
        self.write(10)
        await self.send(503)
        oldest = self.worker.snapshot()['oldest_pending']
        self.assertEqual(oldest, 10)
        self.write(20)
        self.worker.collect()
        self.worker.save()
        self.assertEqual(self.worker.snapshot()['oldest_pending'], oldest)
        self.worker = Delivery(self.root, 'business', ['center', 'node'])
        self.worker.collect()
        self.assertEqual(self.worker.snapshot()['oldest_pending'], oldest)
        await self.send()
        self.assertIsNone(self.worker.snapshot()['oldest_pending'])

    async def test_new_version_resets_retry_count_and_auth_failure_remains_recoverable(self):
        self.write(10)
        await self.send(422)
        await self.send(422)
        self.write(11)
        await self.send(422)
        self.assertEqual(next(iter(self.worker.failures.values()))["attempts"], 1)
        self.assertFalse(self.worker.rejected)
        for _ in range(3):
            await self.send(403)
        self.assertFalse(self.worker.rejected)
        await self.send(200)
        self.assertEqual(self.worker.collect(), [])

    async def test_hundred_thousand_acked_records_do_not_scan_source(self):
        self.worker.ensure_outbox()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany(
                "INSERT INTO business_cache VALUES(1,?,?,?)", [(f"old:{i}", "{}", i + 1) for i in range(100000)]
            )
            db.execute("UPDATE delivery_outbox SET pending=0,ack=updated")
            db.execute("INSERT INTO business_cache VALUES(1,'late','{}',1)")
            plan = db.execute(
                "EXPLAIN QUERY PLAN SELECT shop_id,scope,updated,source,row_id FROM delivery_outbox WHERE lane=? AND pending=1 AND rejected=0 AND retry_at<=? ORDER BY retry_at,updated LIMIT 100",
                ("business", time.time()),
            ).fetchall()
        self.assertTrue(any("delivery_outbox_ready" in r[3] for r in plan), plan)
        start = time.perf_counter()
        self.assertEqual([r["scope"] for r in self.worker.collect()], ["late"])
        self.assertLess(time.perf_counter() - start, 1)

    async def test_center_restore_without_base_requires_full_content(self):
        self.write(100)
        await self.send()
        self.worker.apply_receipts({"records": []})
        self.write(101)
        self.assertEqual((await self.send())[0][0]["scope"], "orders:test")

    async def test_concurrent_publication_keeps_candidate_and_payload_consistent(self):
        self.write(100, value=1)
        self.worker.ensure_outbox()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
        from backend.center.delivery_outbox import connect

        owner = self

        @contextmanager
        def racing(path):
            with connect(path) as db:

                class Proxy:
                    def execute(self, sql, *args):
                        cursor = db.execute(sql, *args)
                        if "SELECT shop_id,scope,updated,source,row_id" not in sql:
                            return cursor

                        class Read:
                            def fetchall(self):
                                rows = cursor.fetchall()
                                owner.write(101, value=2)
                                return rows

                        return Read()

                yield Proxy()

        with patch("backend.center.delivery_outbox.connect", racing):
            records = self.worker.collect()
        self.assertEqual(records[0]["updated"], 100)
        self.assertEqual(json.loads(records[0]["payload"])["rows"][0]["value"], 1)
        self.worker.commit(records)
        current = self.worker.collect()
        self.assertEqual(current[0]["updated"], 101)
        self.assertEqual(json.loads(current[0]["payload"])["rows"][0]["value"], 2)

    async def test_center_restore_with_older_base_and_new_heartbeat_resends_content(self):
        self.write(100)
        await self.send()
        self.write(102, value=2)
        await self.send()
        self.worker.apply_receipts(
            {
                "records": [
                    {"shop": "1", "scope": "orders:test", "updated": 100},
                    {"shop": "1", "scope": "freshness:orders:test", "updated": 103},
                ]
            }
        )
        # No new collection is needed: even a paused source must repair the
        # restored base instead of trusting the newer surviving heartbeat.
        self.assertEqual((await self.send())[0][0]["scope"], "orders:test")

    def test_rejection_is_visible_without_marking_network_failure(self):
        from backend.collector_overview import delivery_hint

        text = delivery_hint(
            {"business": {"pending": 0, "failed": 0, "rejected_count": 2, "rejected_error": "中心回传 HTTP 422"}}
        )
        self.assertIn("中心拒收 2 份", text)
        self.assertIn("HTTP 422", text)


class FreshnessReadTests(unittest.TestCase):
    def test_heartbeat_notification_maps_to_original_business_kind(self):
        from backend.center.business_events import notification

        key = ("node", "1", "freshness:summary:transactions:test")
        value = notification({key: 1}, {key: 2}, [{"node_id": "node", "local_id": 1, "id": 4, "platform": "pdd"}])
        self.assertEqual(value["changes"], [{"kind": "transactions", "shop_id": 4, "platform": "pdd"}])

    def test_lightweight_observation_overlays_only_matching_payload_version(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database("sqlite:///" + str(Path(folder) / "center.sqlite3"))
            try:
                node = database.register("node")["id"]
                base = {
                    "shop": "1",
                    "scope": "transactions:test",
                    "updated": 100,
                    "payload": {"rows": [], "_sync": {"snapshot_started_at": 100}},
                }
                database.ingest(node, [base])
                from backend.center.home_counts import signature

                shops = [{"node_id": node, "local_id": 1}]
                before = signature(database, "transactions", shops)
                database.ingest(
                    node,
                    [
                        {
                            "shop": "1",
                            "scope": "freshness:transactions:test",
                            "updated": 101,
                            "payload": {
                                "content_updated": 100,
                                "collected_at": 101,
                                "_sync": {"snapshot_started_at": 101},
                            },
                        }
                    ],
                )
                record = load(database, [(node, "1", "transactions:test")])[(node, "1")][0]
                self.assertNotEqual(signature(database, "transactions", shops), before)
                self.assertEqual(record["updated"], 101)
                self.assertEqual(record["payload"]["_sync"]["snapshot_started_at"], 101)
                from sqlalchemy import select
                from backend.center.database import snapshots

                with database.engine.connect() as db:
                    self.assertEqual(
                        db.execute(
                            select(snapshots.c.updated).where(snapshots.c.scope == "transactions:test")
                        ).scalar(),
                        100,
                    )
                database.ingest(node, [{**base, "updated": 102}])
                record = load(database, [(node, "1", "transactions:test")])[(node, "1")][0]
                self.assertEqual(record["updated"], 102)
            finally:
                database.engine.dispose()
