import assert from 'node:assert/strict';
import { updateMatches, focusKinds } from '../app/lib/data-updates.mjs';
import { setSnapshotScope, saveSnapshot, readSnapshot, snapshotContext } from '../app/lib/response-snapshots.mjs';
import { databaseReader } from '../app/lib/database-reader.js';
class Storage {
  getItem(k) {
    return this[k] ?? null;
  }
  setItem(k, v) {
    this[k] = v;
  }
  removeItem(k) {
    delete this[k];
  }
}
globalThis.sessionStorage = new Storage();
const event = (shop_id, kind, platform = 'pdd') => ({ changes: [{ shop_id, kind, platform }] });
assert(!updateMatches(event(1, 'reviews'), { kind: 'overview' }));
assert(!updateMatches(event(2, 'aftersales'), { kinds: focusKinds, shopId: 1 }));
assert(!updateMatches(event(1, 'aftersales', 'taobao'), { kinds: focusKinds, platform: 'pdd' }));
assert(updateMatches(event(1, 'aftersales'), { kinds: focusKinds, shopId: 1 }));
assert(updateMatches({ reset: true }, { kind: 'overview' }));
assert(updateMatches(event(0, '*'), { kind: 'overview', shopId: 9 }));
setSnapshotScope('local:test');
const params = new URLSearchParams('shop_id=1&page=1');
saveSnapshot('/api/data/overview', params, {
  sources: [{ shop_id: 1, updated_at: '2026-10-10' }],
  rows: [{ id: 'old' }],
});
assert.equal(readSnapshot('/api/data/overview', params).rows[0].id, 'old');
assert.equal(readSnapshot('/api/data/overview', 'shop_id=2&page=1'), null);
let finish;
const calls = [];
globalThis.fetch = (url, options) => {
  calls.push({ url, options });
  return new Promise((r) => (finish = r));
};
let preview;
const read = databaseReader('/api/data/overview');
const promise = read(params, { onPreview: (d) => (preview = d) });
assert.equal(preview.rows[0].id, 'old'); // already visible before network completes
finish(new Response(JSON.stringify({ sources: [{ updated_at: '2026-10-10' }], rows: [{ id: 'new' }] })));
await promise;
assert.equal(calls.filter((c) => c.options.method === 'POST').length, 0);
setSnapshotScope('remote:other');
assert.equal(readSnapshot('/api/data/overview', params), null);
const context = snapshotContext();
setSnapshotScope('local:test');
saveSnapshot('/api/data/overview', params, { rows: [{ id: 'wrong machine' }] }, context);
assert.equal(readSnapshot('/api/data/overview', params).rows[0].id, 'new');
console.log('PASS scoped updates, immediate preview, no collection on cached reads, machine and query isolation');
