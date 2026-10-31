import assert from 'node:assert/strict';
import { overviewTarget } from '../app/lib/overview-navigation.mjs';
for (const [metric, kind, status] of [
  ['pending_shipping', 'orders', '待发货'],
  ['shipping_due', 'orders', '即将逾期发货'],
  ['pending_aftersales', 'aftersales', '待商家处理'],
  ['aftersales_due', 'aftersales', '待处理即将逾期'],
  ['pending_tickets', 'tickets', '待处理'],
]) {
  const t = overviewTarget(metric, {
    shop_id: '1,2',
    platform: 'pdd,taobao',
    keyword: '旧条件',
    priority_only: true,
    date_from: '2020-01-01',
  });
  assert.equal(t.kind, kind);
  assert.equal(t.filters.status, status);
  assert.equal(t.filters.shop_id, '1,2');
  assert.equal(t.filters.platform, 'pdd,taobao');
  assert.equal(t.filters.keyword, '');
  assert.equal(t.filters.priority_only, false);
  assert.equal(t.filters.date_from, '');
}
assert.equal(overviewTarget('missing'), null);
console.log('5 dashboard routes preserve shop/platform scope and reset conflicting filters.');
