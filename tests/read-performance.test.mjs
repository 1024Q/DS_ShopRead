import test from 'node:test';
import assert from 'node:assert/strict';
import { databaseReader } from '../app/lib/database-reader.js';
import { setSnapshotScope, saveSnapshot, readSnapshot, clearSnapshots } from '../app/lib/response-snapshots.mjs';
import { updateMatches } from '../app/lib/data-updates.mjs';
import { setClientAccess } from '../app/lib/client-access.mjs';
class Storage {
  getItem(k) {
    return this[k] ?? null;
  }
  setItem(k, v) {
    this[k] = String(v);
  }
  removeItem(k) {
    delete this[k];
  }
}
globalThis.sessionStorage = new Storage();
const reply = (d) => new Response(JSON.stringify(d), { headers: { 'Content-Type': 'application/json' } });
const data = { sources: [{ shop_id: 1, updated_at: '2026-10-10T01:00:00+08:00' }], rows: [{ id: 'a' }] };

test('a read-only account never automatically registers missing data or submits manual collection', async () => {
  const previous = globalThis.fetch,
    calls = [];
  setClientAccess({ role: 'user', permissions: { transactions: 'read' } });
  setSnapshotScope('readonly');
  clearSnapshots();
  globalThis.fetch = async (url, options) => {
    calls.push(options?.method);
    return reply({ sources: [{ updated_at: null }], rows: [] });
  };
  try {
    assert.equal((await databaseReader('/api/data/transactions')()).status, 200);
    assert.deepEqual(calls, [undefined]);
    assert.equal((await databaseReader('/api/data/transactions')(new URLSearchParams(), { force: true })).status, 403);
    assert.deepEqual(calls, [undefined]);
  } finally {
    setClientAccess(undefined);
    globalThis.fetch = previous;
  }
});
test('saved database data renders without submitting collection', async () => {
  setSnapshotScope('a');
  clearSnapshots();
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push([url, options.method]);
    return reply(data);
  };
  assert.deepEqual(await (await databaseReader('/api/data/reviews')()).json(), data);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1], undefined);
});
test('missing data returns before a pending background registration', async () => {
  const calls = [];
  let resolve;
  globalThis.fetch = (url, options) => {
    calls.push(options.method);
    return options.method === 'POST'
      ? new Promise((r) => (resolve = r))
      : Promise.resolve(reply({ sources: [{ updated_at: null }], rows: [] }));
  };
  const r = await databaseReader('/api/data/products')();
  assert.equal(r.status, 200);
  assert.deepEqual(calls, [undefined, 'POST']);
  resolve(reply({}));
});
test('manual collection failures are surfaced rather than hidden by cached results', async () => {
  globalThis.fetch = async (url, options) =>
    options.method === 'POST' ? new Response('{"detail":"denied"}', { status: 403 }) : reply(data);
  assert.equal((await databaseReader('/api/data/reviews')(new URLSearchParams(), { force: true })).status, 403);
});
test('preview is immediate while fresh query is pending and keys isolate all filters and machines', async () => {
  setSnapshotScope('machine-a');
  clearSnapshots();
  const params = new URLSearchParams('shop_id=1&platform=pdd&page=1');
  saveSnapshot('/api/data/reviews', params, data);
  let resolve, preview;
  globalThis.fetch = () => new Promise((r) => (resolve = r));
  const loading = databaseReader('/api/data/reviews')(params, { onPreview: (d) => (preview = d) });
  assert.equal(preview.rows[0].id, 'a');
  assert.ok(preview._preview_cached_at);
  assert.equal(readSnapshot('/api/data/reviews', 'shop_id=2&platform=pdd&page=1'), null);
  assert.equal(readSnapshot('/api/data/reviews', 'shop_id=1&platform=pdd&page=2'), null);
  setSnapshotScope('machine-b');
  assert.equal(readSnapshot('/api/data/reviews', params), null);
  setSnapshotScope('machine-a');
  assert.equal(readSnapshot('/api/data/reviews', params).rows[0].id, 'a');
  resolve(reply(data));
  await assert.rejects(loading, { name: 'AbortError' });
});
test('same-day previews expire across business days and mutations invalidate them', () => {
  clearSnapshots();
  setSnapshotScope('expiry');
  const now = Date.now;
  try {
    Date.now = () => 1000000000000;
    saveSnapshot('/api/data/reviews', '', data);
    Date.now = () => 1000000300001;
    assert.equal(readSnapshot('/api/data/reviews', '').rows[0].id, 'a');
    Date.now = () => 1000086400001;
    assert.equal(readSnapshot('/api/data/reviews', ''), null);
  } finally {
    Date.now = now;
  }
  saveSnapshot('/api/data/reviews', '', data);
  clearSnapshots();
  assert.equal(readSnapshot('/api/data/reviews', ''), null);
});
test('overview refresh includes yesterday transactions and respects multi-shop scopes', () => {
  const detail = { changes: [{ kind: 'transactions', shop_id: 22, platform: 'taobao' }] };
  assert.equal(updateMatches(detail, { kind: 'overview', shopId: '1,22', platform: 'pdd,taobao' }), true);
  assert.equal(updateMatches(detail, { kind: 'overview', shopId: '1,2', platform: 'pdd' }), false);
  assert.equal(updateMatches({ reset: true }, { kind: 'overview', shopId: '1,22' }), true);
});
