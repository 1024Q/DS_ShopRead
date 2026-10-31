import test from 'node:test';
import assert from 'node:assert/strict';
import { operationHeaders } from './operation-headers.mjs';
import { fieldsByKind } from './table-sort.mjs';
import { navigationGroups } from './platforms.mjs';

test('every sortable column has a header in the same position (plus the action column)', () => {
  for (const [kind, headers] of Object.entries(operationHeaders)) {
    const fields = fieldsByKind[kind];
    assert.ok(fields, kind);
    assert.equal(headers.at(-1), '操作', kind);
    assert.equal(headers.length - 1, fields.length, kind);
    assert.ok(headers[0].includes('店铺'), kind);
  }
});

test('key columns come first: object, then status, before amounts and times', () => {
  const position = (kind, label) => operationHeaders[kind].indexOf(label);
  assert.ok(position('orders', '订单状态') < position('orders', '实收金额'));
  assert.ok(position('invoices', '开票状态') < position('invoices', '公司主体'));
  assert.ok(position('packages', '包裹状态') < position('packages', '快递公司'));
  assert.ok(position('intercepts', '拦截状态') < position('intercepts', '快递 / 运单号'));
  assert.ok(position('activities', '报名状态') < position('activities', '活动信息'));
});

test('sidebar keeps every entry exactly once', () => {
  const ids = navigationGroups.flatMap(([, items]) => items.map(([id]) => id));
  assert.equal(new Set(ids).size, ids.length);
  for (const id of ['overview', 'orders', 'cost_tables', 'shops', 'settings', 'report_exports', 'navigator'])
    assert.ok(ids.includes(id), id);
  assert.ok(!navigationGroups.some(([label]) => label === '其他功能'));
});
