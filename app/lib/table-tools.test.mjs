import test from 'node:test';
import assert from 'node:assert/strict';
import { compareValues, filterSort, summarize } from './table-tools.mjs';
test('numeric sorting, null last in both directions, textual identifiers preserved', () => {
  const cols = [
      { id: 'sku', type: 'text' },
      { id: 'amount', type: 'number' },
    ],
    rows = [
      { sku: '001', amount: '10' },
      { sku: '0002', amount: '2' },
      { sku: '003', amount: null },
      { sku: '004', amount: 0 },
    ];
  assert.deepEqual(
    filterSort(rows, cols, '', {}, 'amount', 'asc').map((r) => r.amount),
    [0, '2', '10', null],
  );
  assert.deepEqual(
    filterSort(rows, cols, '', {}, 'amount', 'desc').map((r) => r.amount),
    ['10', '2', 0, null],
  );
  assert.equal(filterSort(rows, cols, '001', {}, '', 'asc')[0].sku, '001');
  assert.deepEqual(summarize(rows, cols).amount, { value: 12, missing: 1 });
});
test('column filters combine and summary covers all matched records', () => {
  const cols = [{ id: 'shop' }, { id: 'amount', type: 'number' }],
    rows = Array.from({ length: 150 }, (_, i) => ({ shop: i < 120 ? 'A' : 'B', amount: 1 }));
  const filtered = filterSort(rows, cols, '', { shop: 'A' }, 'amount', 'asc');
  assert.equal(filtered.length, 120);
  assert.equal(summarize(filtered, cols).amount.value, 120);
  assert.ok(compareValues('2026-09-01', '2026-10-01', 'text') < 0);
});
