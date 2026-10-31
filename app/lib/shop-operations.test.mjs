import test from 'node:test';
import assert from 'node:assert/strict';
import { mergeShopOperations, operationsCsv } from './shop-operations.mjs';

test('shop and goods values keep their separate scopes, zeros and missing coverage', () => {
  const shops = [
    { id: 1, name: '甲', platform: 'pdd' },
    { id: 2, name: '乙', platform: 'pdd' },
  ];
  const goods = { metrics: [{ shop_id: '1', summary: { sales: 987, orders: 7, visitors: 0 } }] };
  const home = {
    metrics: [{ shop_id: 1, sales: 12, orders: 0 }],
    sources: [{ shop_id: 1, updated_at: '2026-10-30T12:00:00+08:00' }],
  };
  const ads = { metrics: [{ shop_id: '1', total_spend: 0, ad_spend: 99 }] };
  const refunds = {
    metrics: [
      {
        shop_id: '1',
        refund_date: '2026-10-30',
        refund_basis: 'order_created_success',
        refund_complete: true,
        refund_orders: 1,
        refund_amount: 2,
      },
    ],
  };
  const rows = mergeShopOperations(shops, goods, home, null, ads, refunds, '2026-10-30');
  assert.equal(rows[0].sales, 12);
  assert.equal(rows[0].orders, 0);
  assert.equal(rows[0].goods_summary.sales, 987);
  assert.equal(rows[0].goods_summary.orders, 7);
  assert.equal(rows[0].goods_summary.visitors, 0);
  assert.equal(rows[0].ad_spend, 0);
  assert.equal(rows[0].refund_orders, 1);
  assert.equal(rows[1].sales, undefined);
  assert.equal(rows[1].ad_spend, null);
  assert.equal(rows[1].goods_available, false);
  assert.deepEqual(rows[1].goods_summary, {});
});

test('incomplete refunds cannot enter current-day store totals', () => {
  const refunds = {
    metrics: [
      {
        shop_id: 1,
        refund_date: '2026-10-30',
        refund_basis: 'order_created_success',
        refund_complete: false,
        refund_amount: 3,
      },
    ],
  };
  const [row] = mergeShopOperations([{ id: 1, platform: 'pdd' }], null, null, null, null, refunds, '2026-10-30');
  assert.equal(row.refund_amount, null);
});

test('combined CSV retains missing values, numbers and safely quotes merchant text', () => {
  const csv = operationsCsv(
    ['店铺', '金额', '未知'],
    [
      ['=危险公式', -10, null],
      ['引号"换行\n', 0, ''],
    ],
  );
  assert.ok(csv.startsWith('\ufeff'));
  assert.ok(csv.includes('"\'=危险公式","-10",""'));
  assert.ok(csv.includes('"引号""换行\n","0",""'));
});
