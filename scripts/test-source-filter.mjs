import assert from 'node:assert/strict';
import { filterSources } from '../app/lib/source-filter.mjs';
const row = { shop_id: 1, kind: 'orders', state: 'logged_in', data_status: 'ready', updated_at: 'today' };
const rows = [
  row,
  { ...row, shop_id: 2, data_status: 'running', syncing: true },
  { ...row, shop_id: 3, data_status: 'error', error: 'failed' },
  { ...row, shop_id: 4, state: 'stopped', error: 'old error' },
  { ...row, shop_id: 5, updated_at: null, data_status: 'waiting_login', waiting_login: true },
];
const snapshot = { snapshot_complete: true, rows };
assert.deepEqual(filterSources(snapshot).counts, { all: 5, active: 1, issues: 2, ready: 4 });
assert.equal(filterSources(snapshot, { state: 'active' }).rows[0].shop_id, 2);
assert.equal(filterSources(snapshot, { state: 'issues' }).rows.length, 2);
assert.equal(filterSources(snapshot, { state: 'recorded' }).rows.length, 4);
assert.equal(filterSources(snapshot, { state: 'issues', shop: '3', keyword: 'failed' }).total, 1);
const large = { rows: Array.from({ length: 1000 }, (_, i) => ({ ...row, shop_id: i + 1, operators: ['张三'] })) };
let start = performance.now();
for (let i = 0; i < 20; i++) filterSources(large, { state: 'recorded', page: 20, keyword: '张三' });
assert.equal(filterSources(large, { page: 20 }).rows.length, 30);
assert.equal(filterSources(large, { page: 100 }).page, 34);
assert.equal(filterSources(large, { sort: { field: 'shop_id', direction: 'desc' } }).rows[0].shop_id, 1000);
console.log(
  'PASS source filters, counts, pagination, sorting; 1000 sources x 20 filters:',
  Math.round(performance.now() - start),
  'ms',
);
