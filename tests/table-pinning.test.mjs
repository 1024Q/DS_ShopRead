import test from 'node:test';
import assert from 'node:assert/strict';
import { pinnedColumn, columnClass } from '../app/lib/table-pinning.mjs';
test('the React cell and native preferences agree on fixed columns', () => {
  const labels = ['店铺', '运营', '成交金额', '操作'];
  assert.deepEqual(labels.map(pinnedColumn), [true, true, false, false]);
  assert.equal(columnClass('运营', 1, 4), 'pinned-column');
  assert.equal(columnClass('操作', 3, 4), 'action-column');
});
