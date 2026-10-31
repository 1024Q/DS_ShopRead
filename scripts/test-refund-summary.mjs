import assert from 'node:assert/strict';
import { refundSummary } from '../app/lib/refund-summary.mjs';
const today = '2026-10-11',
  shops = [
    { id: 1, platform: 'pdd' },
    { id: 2, platform: 'taobao' },
    { id: 3, platform: 'jd' },
  ];
const data = {
  scope: { day: today, trade_range: 'realtime' },
  metrics: [
    { shop_id: 1, summary: { refund_amount: 999, refund_orders: 99 } },
    { shop_id: 2, summary: { refund_amount: 10, refund_orders: null } },
  ],
};
const orders = {
  metrics: [
    {
      shop_id: 1,
      refund_date: today,
      refund_basis: 'order_created_success',
      refund_complete: true,
      refund_amount: 12.34,
      refund_orders: 2,
    },
  ],
};
assert.deepEqual(
  refundSummary(data, shops, '', today, orders).groups.map((r) => [r.date, r.amount, r.orders]),
  [[today, 22.34, 2]],
);
assert.equal(refundSummary(data, shops, '1', today).groups[0].amount, null);
assert.equal(refundSummary(data, shops, '3', today, orders).groups.length, 0);
assert.equal(refundSummary(data, shops, '', '2026-10-12', orders).groups[0].amount, null);
assert.equal(
  refundSummary(data, shops, '1', today, { metrics: [{ ...orders.metrics[0], refund_amount: 0, refund_orders: 0 }] })
    .groups[0].orders,
  0,
);
assert.equal(
  refundSummary(data, shops, '1', today, { metrics: [{ ...orders.metrics[0], refund_complete: false }] }).groups[0]
    .orders,
  null,
);
console.log('PASS current-day order/refund join, partial coverage, zero, scope and midnight reset');
