import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import asynccontextmanager, closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend import auto_refunds, collection, request_reuse, status_cache
from backend.store import Store
from backend.tests.test_auto_refunds import CASE


class RequestCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_request_rechecks_cooldown_before_dispatch(self):
        root = SimpleNamespace()
        entered = asyncio.Event()

        async def first():
            entered.set()
            await asyncio.sleep(0.02)
            return {"success": False, "errorMsg": "请求频繁"}

        following = AsyncMock()
        with patch.dict('os.environ', {'ECMP_READ_MIN_INTERVAL': '0.1'}):
            running = asyncio.create_task(request_reuse.paced(root, '/same', first))
            await entered.wait()
            with self.assertRaisesRegex(ValueError, '冷却'):
                await request_reuse.paced(root, '/same', following)
            await running
        following.assert_not_awaited()

    async def test_other_endpoint_has_short_shop_cooldown_not_five_minutes(self):
        from backend.collection_errors import classify

        root = SimpleNamespace()
        request_reuse.enter_cooldown(root, '/limited')
        with self.assertRaises(request_reuse.ShopReadCooldown) as caught:
            await request_reuse.paced(root, '/other', AsyncMock())
        self.assertEqual(classify(caught.exception), ('shop_cooldown', False))
        self.assertLessEqual(caught.exception.retry_after, 20)
        self.assertEqual(collection.retry_delay(5, 'shop_cooldown'), 20)


class RefundRecordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SimpleNamespace(directory=Path(self.temp.name))
        self.shop = {'id': 21, 'platform': 'pdd'}

    async def asyncTearDown(self):
        self.temp.cleanup()

    def write(self, case, record):
        path = auto_refunds.record_path(self.store, 21, case)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding='utf-8')
        return path

    async def test_absent_wrong_identity_or_malformed_response_never_proves_success(self):
        original = {'case_id': '123', 'order_number': 'order-1', 'state': 'unknown',
                    'created_at': time.time() - 500, 'version': 4}
        for result in ({'list': []}, {'list': [{**CASE, 'orderSn': 'other', 'afterSalesTitle': '退款成功'}]}, {}):
            path = self.write(123, original)
            with patch('backend.operations.uncached_read', AsyncMock(return_value=result)), patch('backend.auto_refunds.request', AsyncMock()) as submit:
                checked = await auto_refunds.verify(SimpleNamespace(), self.store, self.shop, path, original)
            self.assertEqual(checked['state'], 'unknown')
            submit.assert_not_awaited()

    async def test_invalid_record_metadata_is_isolated_without_hiding_good_records(self):
        self.write(1, {'case_id': '1', 'state': 'accepted', 'created_at': 'invalid'})
        self.write(2, {'case_id': '2', 'state': 'accepted', 'created_at': 123})
        rows = auto_refunds.records(self.store, 21)['rows']
        self.assertEqual({r['case_id']: r['state'] for r in rows}, {'1': 'review', '2': 'accepted'})

    async def test_timestamp_ties_do_not_drop_records_between_pages(self):
        for case in range(1, 4):
            self.write(case, {'case_id': str(case), 'state': 'accepted', 'created_at': 123})
        first = auto_refunds.records(self.store, 21, limit=2)
        second = auto_refunds.records(self.store, 21, limit=2, before=first['next_before'], before_case_id=first['next_case_id'])
        self.assertEqual([r['case_id'] for r in first['rows'] + second['rows']], ['3', '2', '1'])

    async def test_partial_post_refund_refresh_cannot_replace_complete_saved_data(self):
        payload = {'rows': [{'id': '123', 'status': '平台处理中'}], 'complete': True}
        session = SimpleNamespace(shop=self.shop)
        page = SimpleNamespace(open=AsyncMock())

        @asynccontextmanager
        async def borrow(*args):
            yield page

        with (patch('backend.read_page_pool.borrow', borrow),
              patch('backend.auto_refunds.process', AsyncMock(return_value=(True, []))),
              patch('backend.auto_refunds.reconcile', AsyncMock()),
              patch('backend.operations.collect_operations', AsyncMock(return_value={'rows': [], 'complete': False}))):
            result, changed = await auto_refunds.apply(session, self.store, 'aftersales', {'_attention': True}, payload)
        self.assertIs(result, payload)
        self.assertFalse(changed)


class CommitInvalidationTests(unittest.TestCase):
    def test_publication_invalidates_snapshot_loaded_before_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            sid = store.add('cache-test', 'password')
            original = store.write_connection

            @contextmanager
            def transaction():
                with original() as db:
                    yield db
                    status_cache.get(store, sid, lambda: 'pre-commit')

            with patch.object(store, 'write_connection', transaction), patch.object(store, '_prune_cache'):
                store.write_cache(sid, 'reviews:test', {'rows': [], 'complete': True})
            self.assertEqual(status_cache.get(store, sid, lambda: 'fresh'), 'fresh')

    def test_forget_and_touch_all_fence_inflight_loads(self):
        for invalidate in (lambda s: status_cache.forget(s, 1), status_cache.touch_all):
            store = SimpleNamespace(directory='fixture-' + str(id(invalidate)))

            def load():
                invalidate(store)
                return 'old'

            status_cache.get(store, 1, load)
            self.assertEqual(status_cache.get(store, 1, lambda: 'fresh'), 'fresh')


class OutboxFairnessTests(unittest.TestCase):
    def test_large_older_shop_backlog_cannot_hide_other_shops_from_candidate_limit(self):
        from backend.center.replication_worker import Delivery

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with closing(sqlite3.connect(root / 'shops.sqlite3')) as db, db:
                db.execute('CREATE TABLE business_cache(shop_id INTEGER,cache_key TEXT,payload TEXT,updated REAL,PRIMARY KEY(shop_id,cache_key))')
                db.executemany('INSERT INTO business_cache VALUES(?,?,?,?)', [
                    (1, f'overview:{i}', json.dumps({'rows': [{'id': str(i)}]}), i + 1) for i in range(350)
                ])
                db.execute('INSERT INTO business_cache VALUES(2,?,?,?)', ('overview:other', json.dumps({'rows': [{'id': 'other'}]}), 10000))
            worker = Delivery(root, 'business', ['center', 'node'])
            self.assertIn('2', {r['shop'] for r in worker.collect(now=10**12)})


if __name__ == '__main__':
    unittest.main()
