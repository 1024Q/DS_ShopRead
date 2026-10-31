import assert from 'node:assert/strict';
import { retryUrl, retryCandidates, retrySources, retrySourceGroup } from '../app/lib/source-retry.mjs';
const source = (shop_id, extra = {}) => ({
  shop_id,
  kind: 'reviews',
  platform: 'pdd',
  error: '读取失败',
  scope: { date_from: '2026-09-01', date_to: '2026-09-02', _exact_dates: true, stars: [1, 2], reply: 'unreplied' },
  ...extra,
});
const normal = source(1),
  attention = source(2, { kind: 'evidence', scope: { _attention: true } }),
  period = source(3, { kind: 'tax', scope: { period: '2026-09' } });
const rows = [
  normal,
  normal,
  attention,
  period,
  source(4, { waiting_login: true }),
  source(5, { data_status: 'queued' }),
  source(6, { syncing: true }),
  source(7, { error: null, data_status: 'ready' }),
  source(8, { error: null, data_status: 'blocked' }),
];
assert.equal(retryCandidates(rows).length, 4);
const query = new URL(retryUrl(normal), 'http://localhost').searchParams;
for (const [k, v] of Object.entries({
  force: 'true',
  shop_id: '1',
  platform: 'pdd',
  exact_dates: 'true',
  date_from: '2026-09-01',
  date_to: '2026-09-02',
  stars: '1,2',
  reply: 'unreplied',
}))
  assert.equal(query.get(k), v);
assert.equal(retryUrl(attention), '/api/attention/sync?shop_id=2&kind=evidence');
assert.match(retryUrl(period), /period=2026-09/);
let active = 0,
  maxActive = 0;
const requests = [],
  progress = [];
const result = await retrySources(
  rows,
  (r) => progress.push(r.completed),
  async (url, options) => {
    assert.equal(options.method, 'POST');
    active++;
    maxActive = Math.max(maxActive, active);
    requests.push(url);
    await new Promise((r) => setTimeout(r, 5));
    active--;
    return {
      ok: !url.includes('shop_id=2'),
      json: async () => (url.includes('shop_id=2') ? { detail: '权限不足' } : { tasks: [{}] }),
    };
  },
);
assert.equal(maxActive, 1);
assert.equal(requests.length, 4);
assert.equal(result.submitted, 3);
assert.equal(result.failed.length, 1);
assert.equal(result.failed[0].message, '权限不足');
assert.deepEqual(progress, [1, 2, 3, 4]);
assert.equal(
  (await retrySources([source(9)], null, async () => ({ ok: true, json: async () => ({ tasks: [] }) }))).failed.length,
  1,
);
console.log(
  'PASS: scoped retry, deduplication, login/active exclusions, sequential submission and partial failure reporting',
);
const scoped = source(11, {
  error: null,
  data_status: 'ready',
  failed_scopes: [
    { cache_key: 'reviews:a', scope: { date_from: '2026-09-01' } },
    { cache_key: 'reviews:b', scope: { date_from: '2026-09-02' } },
  ],
});
assert.equal(retryCandidates([scoped]).length, 2);
assert.equal(retryCandidates([scoped, { ...scoped, state: 'stopped' }]).length, 2);
assert.match(retryUrl(retryCandidates([scoped])[0]), /retry-scope\?shop_id=11&cache_key=reviews%3Aa/);
console.log('PASS: every saved failed range retries independently, stopped shops excluded');
const calls = [];
await retrySourceGroup({ ...scoped, failed_scopes: undefined, failed_scope_count: 2 }, async (url, options) => {
  calls.push(url);
  return {
    ok: true,
    json: async () => (options?.method === 'POST' ? { queued: true } : { rows: [{ ...scoped, syncing: true }] }),
  };
});
assert.equal(calls.length, 3);
assert.match(calls[1], /reviews%3Aa/);
assert.match(calls[2], /reviews%3Ab/);
assert.equal(
  (
    await retrySources([source(9)], null, async () => ({
      ok: true,
      json: async () => ({ tasks: [{ syncing: false }] }),
    }))
  ).failed.length,
  1,
);
console.log('PASS: row retry resolves every failed scope and rejects unqueued responses');
