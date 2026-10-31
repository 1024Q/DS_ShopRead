import test from 'node:test';
import assert from 'node:assert/strict';
import { sourceIssue, retryUrl, retryCandidates, retrySource } from './source-retry.mjs';

test('stopped shops are never reported as issues or retried', () => {
  assert.equal(sourceIssue({ state: 'stopped', error: 'x' }), false);
  assert.equal(sourceIssue({ state: 'logged_in', error: 'x' }), true);
  assert.deepEqual(retryCandidates([{ state: 'stopped', error: 'x', shop_id: 1, kind: 'orders' }]), []);
});

test('exact saved scope is retried by cache key, never widened', () => {
  assert.equal(
    retryUrl({ shop_id: 3, cache_key: 'orders:abc' }),
    '/api/system/retry-scope?shop_id=3&cache_key=orders%3Aabc',
  );
  const url = new URL(
    retryUrl({ shop_id: 3, kind: 'orders', scope: { date_from: '2026-10-01', _exact_dates: true } }),
    'http://x',
  );
  assert.equal(url.pathname, '/api/data/orders/sync');
  assert.equal(url.searchParams.get('date_from'), '2026-10-01');
  assert.equal(url.searchParams.get('exact_dates'), 'true');
});

test('failed scopes expand and duplicates collapse', () => {
  const source = {
    shop_id: 1,
    kind: 'orders',
    state: 'logged_in',
    failed_scopes: [{ cache_key: 'a' }, { cache_key: 'a' }, { cache_key: 'b' }],
  };
  assert.deepEqual(
    retryCandidates([source]).map((s) => s.cache_key),
    ['a', 'b'],
  );
});

test('retry reports when nothing entered the queue', async () => {
  const reply =
    (body, ok = true) =>
    async () => ({ ok, json: async () => body });
  await assert.rejects(retrySource({ shop_id: 1, cache_key: 'a' }, reply({ tasks: [] })), /没有可采集任务/);
  await assert.rejects(retrySource({ shop_id: 1, cache_key: 'a' }, reply({ detail: '忙' }, false)), /忙/);
  assert.deepEqual(await retrySource({ shop_id: 1, cache_key: 'a' }, reply({ tasks: [{ syncing: true }] })), {
    tasks: [{ syncing: true }],
  });
});
