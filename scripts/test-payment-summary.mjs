import assert from 'node:assert/strict';
import { paymentSummary } from '../app/lib/payment-summary.mjs';
const today = '2026-10-09',
  shops = [
    { id: 1, platform: 'pdd' },
    { id: 2, platform: 'taobao' },
    { id: 3, platform: 'jd' },
  ];
const data = {
  scope: { day: today, trade_range: 'realtime' },
  metrics: [
    { shop_id: 1, summary: { sales: 0.1, orders: 0, buyers: 0 } },
    { shop_id: 2, summary: { sales: 0.2, orders: 3, buyers: 2 } },
    { shop_id: 99, summary: { sales: 9999, orders: 999, buyers: 999 } },
  ],
};
let result = paymentSummary(data, shops, '', today);
assert.deepEqual(result.totals, { paid_sales: 0.3, paid_orders: 3, paid_buyers: 2 });
assert.equal(result.coverage.paid_orders, 2);
assert.equal(result.count, 3);
assert.equal(result.rows[2].paid_sales, null);
assert.equal(paymentSummary(data, shops, '1', today).totals.paid_sales, 0.1);
assert.equal(paymentSummary(data, [shops[1]], '', today).totals.paid_sales, 0.2);
assert.equal(paymentSummary(data, shops, '3', today).totals.paid_sales, null);
assert.equal(paymentSummary(data, shops, '', '2026-10-10').totals.paid_sales, null);
assert.equal(
  paymentSummary({ ...data, scope: { day: today, trade_range: 'yesterday' } }, shops, '', today).totals.paid_sales,
  null,
);
assert.equal(
  paymentSummary(
    { ...data, metrics: [{ shop_id: 1, summary: { sales: NaN, orders: true, buyers: '5' } }] },
    shops,
    '',
    today,
  ).coverage.paid_buyers,
  0,
);
assert.equal(paymentSummary(null, shops, '', today).totals.paid_orders, null);
console.log('PASS payment aggregation: selected shops/platforms, zero, missing, precision, and date boundaries');
