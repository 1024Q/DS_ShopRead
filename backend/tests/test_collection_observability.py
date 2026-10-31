import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend import collection_metrics as metrics, request_reuse as reuse
from backend.operations import ENDPOINTS

PATH = ENDPOINTS['aftersales']


class SafeReuseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict('os.environ', {'ECMP_READ_MIN_INTERVAL': '0'})
        self.env.start()
        self.root = SimpleNamespace(shop={'id': 1, 'platform': 'pdd'}, state='logged_in', port=9001)

    async def asyncTearDown(self):
        self.env.stop()

    async def test_cancel_producer_preserves_page_until_shared_read_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def remote():
            entered.set(); await release.wait(); return {'list': [1]}
        fetch = AsyncMock(side_effect=remote)
        producer = asyncio.create_task(reuse.read(self.root, PATH, {'page': 1}, fetch))
        await entered.wait()
        sibling = asyncio.create_task(reuse.read(self.root, PATH, {'page': 1}, fetch))
        await asyncio.sleep(0)
        producer.cancel(); await asyncio.sleep(0)
        self.assertFalse(producer.done())
        release.set()
        self.assertEqual(await sibling, {'list': [1]})
        with self.assertRaises(asyncio.CancelledError):await producer
        self.assertEqual(fetch.await_count, 1)

    async def test_cancel_subscriber_does_not_cancel_producer(self):
        release = asyncio.Event()
        async def remote():await release.wait(); return {'list': [1]}
        fetch = AsyncMock(side_effect=remote)
        producer = asyncio.create_task(reuse.read(self.root, PATH, {}, fetch))
        await asyncio.sleep(0)
        sibling = asyncio.create_task(reuse.read(self.root, PATH, {}, fetch))
        await asyncio.sleep(0)
        sibling.cancel()
        with self.assertRaises(asyncio.CancelledError):await sibling
        release.set(); self.assertEqual(await producer, {'list': [1]})
        self.assertEqual(fetch.await_count, 1)

    async def test_parallel_failure_is_shared_but_never_cached(self):
        async def remote():await asyncio.sleep(.01); raise ValueError('fixture')
        fetch = AsyncMock(side_effect=remote)
        result = await asyncio.gather(*(reuse.read(self.root, PATH, {}, fetch) for _ in range(5)), return_exceptions=True)
        self.assertTrue(all(isinstance(v, ValueError) for v in result))
        self.assertEqual(fetch.await_count, 1)
        with self.assertRaises(ValueError):await reuse.read(self.root, PATH, {}, fetch)
        self.assertEqual(fetch.await_count, 2)

    async def test_complete_partial_result_is_shared_once_but_not_replayed(self):
        async def remote():await asyncio.sleep(.01); return {'complete': False, 'rows': [1]}
        fetch = AsyncMock(side_effect=remote)
        await asyncio.gather(*(reuse.complete(self.root, 'aftersales', {'start': 1, 'end': 2}, fetch) for _ in range(5)))
        self.assertEqual(fetch.await_count, 1)
        self.assertIsNone(reuse.lookup(self.root, 'aftersales', {'start': 1, 'end': 2}))
        await reuse.complete(self.root, 'aftersales', {'start': 1, 'end': 2}, fetch)
        self.assertEqual(fetch.await_count, 2)

    async def test_mutations_and_unknown_interfaces_are_never_cached_or_joined(self):
        fetch = AsyncMock(return_value={'success': True})
        for path in ('/mercury/mms/afterSales/agreeRefund', '/mercury/mms/afterSales/agreeRefundPreCheck', '/unverified'):
            await asyncio.gather(*(reuse.read(self.root, path, {}, fetch) for _ in range(3)))
        self.assertEqual(fetch.await_count, 9)
        await asyncio.gather(*(reuse.complete(self.root, 'agreeRefund', {}, fetch) for _ in range(3)))
        self.assertEqual(fetch.await_count, 12)

    async def test_sole_cancelled_reader_cancels_its_own_unneeded_remote_read(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def remote():
            entered.set()
            try:await asyncio.sleep(100)
            finally:cancelled.set()
        task=asyncio.create_task(reuse.read(self.root, PATH, {}, remote))
        await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task, 1)
        self.assertTrue(cancelled.is_set())

    async def test_identity_range_and_parameters_cannot_reuse_each_other(self):
        fetch = AsyncMock(return_value={'complete': True, 'rows': []})
        for opts in ({'start': 1, 'end': 2}, {'start': 2, 'end': 3}, {'start': 1, 'end': 2, '_attention': True}):
            await reuse.complete(self.root, 'aftersales', opts, fetch)
        self.root.shop['id'] = 2
        await reuse.complete(self.root, 'aftersales', {'start': 1, 'end': 2}, fetch)
        self.root.shop['platform'] = 'jd'
        await reuse.complete(self.root, 'aftersales', {'start': 1, 'end': 2}, fetch)
        self.assertEqual(fetch.await_count, 5)

    async def test_financial_invalidation_prevents_inflight_old_response_recaching(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def remote():entered.set(); await release.wait(); return {'list': ['old']}
        fetch = AsyncMock(side_effect=remote)
        first = asyncio.create_task(reuse.read(self.root, PATH, {}, fetch))
        await entered.wait(); reuse.invalidate(self.root); release.set(); await first
        fetch.side_effect = None; fetch.return_value = {'list': ['new']}
        self.assertEqual(await reuse.read(self.root, PATH, {}, fetch), {'list': ['new']})
        self.assertEqual(fetch.await_count, 2)


class MetricsTests(unittest.TestCase):
    def test_nested_phases_do_not_double_count_wall_clock(self):
        meta={};tracker=metrics.Tracker(None,{'id':1},'orders:x','orders',{}, {},meta)
        started=time.monotonic()
        with metrics.phase('page_load'):
            time.sleep(.01)
            with metrics.phase('query'):time.sleep(.02)
            time.sleep(.01)
        total=time.monotonic()-started
        self.assertLessEqual(sum(meta['timing_seconds'].values()),total+.005)
        self.assertGreater(meta['timing_seconds']['query'],.015)
        tracker.finish('success')
    def test_phase_clock_and_file_only_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SimpleNamespace(directory=Path(folder))
            meta = {}
            tracker = metrics.Tracker(store, {'id': 1, 'name': 'fixture'}, 'aftersales:test', 'aftersales', {}, {}, meta)
            with metrics.phase('query', '查询售后'):
                time.sleep(.06)
                self.assertGreater(metrics.fields(meta)['timing_current']['elapsed_seconds'], 0)
                metrics.count('network_queries')
            tracker.finish('failed')
            r = json.loads((Path(folder)/'collection-performance.json').read_text())['rows'][0]
            self.assertEqual(r['result'], 'failed')
            self.assertGreater(r['timing_seconds']['query'], .005)
            self.assertEqual(r['reuse_counts']['network_queries'], 1)
            self.assertEqual(list(Path(folder).glob('*.sqlite*')), [])

    def test_file_write_failure_cannot_fail_business_task(self):
        store = SimpleNamespace(directory=Path('unused'))
        tracker = metrics.Tracker(store, {'id': 1}, 'reviews:x', 'reviews', {}, {}, {})
        with patch('backend.collector_logs.DailyLog.write', side_effect=PermissionError()):tracker.finish('success')

    def test_upload_requires_matching_new_source_ack(self):
        with tempfile.TemporaryDirectory() as folder:
            directory=Path(folder);service=SimpleNamespace(store=SimpleNamespace(directory=directory))
            meta={'delivery_scope':'x','started_at':time.time()-2,'finished_at':time.time()-1,'result':'success'}
            metrics.delivery(directory/'shops'/'1', 'business', [{'shop':1,'scope':'x','updated':time.time()-1}], .1)
            result=metrics.enrich(service,1,'x',{}, {(1,'x'):meta})
            self.assertEqual(result['upload_timing']['state'],'confirmed')
            meta['started_at']=time.time()+1
            self.assertEqual(metrics.enrich(service,1,'x',{}, {(1,'x'):meta})['upload_timing']['state'],'pending')
