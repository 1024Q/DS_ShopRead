import test from 'node:test';
import assert from 'node:assert/strict';
import {
  overviewCounts,
  countBasisLabel,
  countBasisDetail,
  countBasisBrief,
  listStateLabel,
} from './overview-counts.mjs';
const stamp = '2026-10-27T12:00:00+08:00';
const report = (rows) => ({
  home_counts: { rows, snapshot_at: stamp },
  scope: { status: '待发货', date_from: '2026-07-30', date_to: '2026-10-27' },
});

test('complete historical lists disclose paused and offline sources without changing counts', () => {
  const value = overviewCounts(
    [
      { shop_id: 1, pending_shipping: 20 },
      { shop_id: 2, pending_shipping: 10 },
    ],
    {
      pending_shipping: report([
        {
          shop_id: 1,
          count: 4,
          verified: true,
          updated_at: stamp,
          freshness_status: 'paused',
          freshness_reason: '定时任务已暂停',
        },
        {
          shop_id: 2,
          count: 2,
          verified: true,
          updated_at: stamp,
          freshness_status: 'offline',
          freshness_reason: '节点离线',
        },
      ]),
    },
  );
  assert.equal(value.totals.pending_shipping, 30);
  assert.equal(value.metrics.pending_shipping.list_count, 6);
  assert.match(listStateLabel(value.metrics.pending_shipping), /范围完整.*1 家定时已暂停.*1 家节点离线/);
  assert.match(countBasisDetail(value.rows[0].count_basis.pending_shipping), /定时任务已暂停/);
});

test('complete list and platform card remain separately identified, including zero', () => {
  const value = overviewCounts([{ shop_id: 1, pending_shipping: 22, updated_at: stamp }], {
    pending_shipping: report([{ shop_id: 1, count: 0, verified: true, updated_at: stamp }]),
  });
  assert.equal(value.rows[0].pending_shipping, 22);
  assert.equal(value.totals.pending_shipping, 22);
  assert.equal(value.metrics.pending_shipping.list_count, 0);
  assert.equal(value.rows[0].count_basis.pending_shipping.list_count, 0);
  assert.equal(value.metrics.pending_shipping.basis, 'platform_home');
  assert.equal(value.metrics.pending_shipping.verified, true);
});
test('partial or scope-unverified lists preserve platform figures and explain mismatch', () => {
  for (const saved of [
    { complete: true, count: 2 },
    { verified: false, count: 3, reason: '仅有即将逾期范围' },
  ]) {
    const value = overviewCounts([{ shop_id: 1, pending_tickets: 17, updated_at: stamp }], {
      pending_tickets: report([{ shop_id: 1, ...saved, updated_at: stamp }]),
    });
    assert.equal(value.rows[0].pending_tickets, 17);
    assert.equal(value.totals.pending_tickets, 17);
    assert.equal(value.metrics.pending_tickets.basis, 'platform_home');
    assert.match(countBasisDetail(value.rows[0].count_basis.pending_tickets), /数量可能不同/);
  }
});
test('partial shop coverage preserves original totals and discloses list coverage separately', () => {
  const value = overviewCounts(
    [
      { shop_id: 1, pending_shipping: 20, updated_at: stamp },
      { shop_id: 2, pending_shipping: 7, updated_at: stamp },
    ],
    {
      pending_shipping: report([
        { shop_id: 1, count: 4, verified: true, updated_at: stamp },
        { shop_id: 2, count: 1, verified: false, updated_at: stamp },
      ]),
    },
  );
  assert.equal(value.totals.pending_shipping, 27);
  assert.deepEqual(
    value.rows.map((row) => row.pending_shipping),
    [20, 7],
  );
  assert.equal(value.metrics.pending_shipping.list_count, 5);
  assert.equal(value.metrics.pending_shipping.verified, false);
  assert.match(countBasisLabel(value.metrics.pending_shipping), /平台首页/);
});
test('urgent counters disclose partial snapshots and missing is never fabricated zero', () => {
  const value = overviewCounts([{ shop_id: 1 }], {
    shipping_due: report([{ shop_id: 1, count: 2, verified: false, updated_at: stamp }]),
  });
  assert.equal(value.totals.shipping_due, 2);
  assert.equal(value.metrics.shipping_due.basis, 'partial_list');
  assert.equal(value.totals.aftersales_due, null);
  assert.equal(value.metrics.aftersales_due.missing, 1);
});
test('mixed complete and partial warning lists never claim platform homepage figures', () => {
  const value = overviewCounts([{ shop_id: 1 }, { shop_id: 2 }], {
    shipping_due: report([
      { shop_id: 1, count: 2, verified: true, updated_at: stamp },
      { shop_id: 2, count: 3, verified: false, updated_at: stamp },
    ]),
  });
  assert.equal(value.totals.shipping_due, 5);
  assert.match(countBasisLabel(value.metrics.shipping_due), /列表未读取完整/);
  assert.doesNotMatch(countBasisLabel(value.metrics.shipping_due), /平台首页/);
});

test('card status stays one short phrase while the hover text keeps the full basis', () => {
  assert.equal(countBasisBrief({}), '尚未读取');
  assert.equal(countBasisBrief({ basis: 'platform_home', missing: 5 }), '5 家缺数据');
  assert.equal(countBasisBrief({ basis: 'partial_list' }), '列表未完整');
  assert.equal(countBasisBrief({ basis: 'list', verified: true }), '');
  assert.match(
    countBasisDetail({ basis: 'platform_home', list_count: 824, scope: { status: '待发货' } }),
    /已存列表待发货 824 条/,
  );
});
