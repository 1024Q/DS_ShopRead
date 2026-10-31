import assert from 'node:assert/strict';
import { createElement as h, Fragment } from 'react';
import { databaseReader, dataScope } from '../app/lib/database-reader.js';
import { rowValues, visibleRange } from '../app/lib/table-values.mjs';
import { compareValues } from '../app/lib/table-sort.mjs';
const calls = [];
let release;
globalThis.fetch = async (url, options) => {
  calls.push({ url, options });
  if (options.method === 'POST') await new Promise((resolve) => (release = resolve));
  return new Response(JSON.stringify({ sources: [{ updated_at: 'saved' }] }), { status: 200 });
};
const read = databaseReader('/api/data/products'),
  first = new AbortController();
const old = read(new URLSearchParams('shop_id=1&sort_by=sales&page=2'), { signal: first.signal }).catch((e) => e.name);
const latest = read(new URLSearchParams('shop_id=1&sort_by=stock&page=1'));
first.abort();
assert.equal(await old, 'AbortError');
await latest;
assert.equal(calls.filter((c) => c.options.method === 'POST').length, 0);
assert.equal(calls.filter((c) => c.options.method !== 'POST').length, 2);
await read(new URLSearchParams('shop_id=1&sort_by=name&page=3&keyword=测试'));
assert.equal(calls.filter((c) => c.options.method === 'POST').length, 0);
const forced = read(new URLSearchParams('shop_id=1'), { force: true });
release();
await forced;
assert.equal(calls.filter((c) => c.options.method === 'POST').length, 1);
assert.equal(dataScope('shop_id=1&page=9&sort_by=stock'), dataScope('sort_by=name&shop_id=1&page=1'));
assert.notEqual(dataScope('shop_id=1'), dataScope('shop_id=2'));
assert.equal(compareValues('¥1,234.50', '¥12.3'), 1222.2);
assert(compareValues('10000000000000001', '10000000000000002') < 0);
assert(compareValues(null, 3, 'desc') > 0);
assert.deepEqual(
  rowValues(
    h(
      Fragment,
      null,
      h(
        'tr',
        null,
        h('td', { 'data-sort-value': 123 }, '¥123.00'),
        h('td', null, h('strong', null, '测试店铺')),
        h(
          'td',
          null,
          h(function Product() {}, { row: { name: '商品名' } }),
        ),
      ),
    ),
  ),
  [123, '测试店铺', '商品名'],
);
assert.deepEqual(visibleRange([0, 80, 160, 240, 320], 0, 80, 0), [0, 1]);
const offsets = Array.from({ length: 10001 }, (_, i) => i * 80),
  [start, end] = visibleRange(offsets, 790000, 560);
assert(start > 9800 && end - start < 25 && end <= 10000);
console.log(
  'PASS demand deduplication, cancellation, forced sync, scope isolation, raw value sorting and virtual range',
);
