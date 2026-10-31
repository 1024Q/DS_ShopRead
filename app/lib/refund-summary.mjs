import { inScope } from './filter-scope.mjs';
const valid = (v) => typeof v === 'number' && Number.isFinite(v) && v >= 0;
export function refundSummary(data, shops, shopId, today, orderRefunds) {
  const selected = new Set(shops.filter((s) => inScope(shopId, s.id)).map((s) => String(s.id)));
  const yesterday = new Date(Date.parse(today + 'T00:00:00+08:00') - 86400000 + 8 * 3600000).toISOString().slice(0, 10);
  const rows = shops
    .filter((s) => selected.has(String(s.id)))
    .map((shop) => {
      const metric =
        data?.scope?.day === today && data?.scope?.trade_range === 'realtime'
          ? data?.metrics?.find((m) => String(m.shop_id) === String(shop.id))
          : null;
      if ((shop.platform || 'pdd') === 'pdd') {
        const refund = orderRefunds?.metrics?.find(
          (m) =>
            String(m.shop_id) === String(shop.id) &&
            m.refund_date === today &&
            m.refund_basis === 'order_created_success' &&
            m.refund_complete,
        );
        return {
          shop_id: shop.id,
          platform: 'pdd',
          date: today,
          refund_amount: valid(refund?.refund_amount) ? refund.refund_amount : null,
          refund_orders: valid(refund?.refund_orders) ? refund.refund_orders : null,
        };
      }
      return {
        shop_id: shop.id,
        platform: shop.platform,
        date: shop.platform === 'taobao' ? today : null,
        refund_amount: valid(metric?.summary?.refund_amount) ? metric.summary.refund_amount : null,
        refund_orders: valid(metric?.summary?.refund_orders) ? metric.summary.refund_orders : null,
      };
    });
  const groups = [today, yesterday]
    .map((date) => {
      const group = rows.filter((r) => r.date === date),
        amounts = group.map((r) => r.refund_amount).filter(valid),
        orders = group.map((r) => r.refund_orders).filter(valid);
      return {
        date,
        label: date === today ? '今日退款' : '昨日退款',
        count: group.length,
        amount: amounts.length ? amounts.reduce((s, v) => s + Math.round(v * 100), 0) / 100 : null,
        orders: orders.length ? orders.reduce((s, v) => s + v, 0) : null,
        amountCoverage: amounts.length,
        orderCoverage: orders.length,
      };
    })
    .filter((g) => g.count);
  return { rows, groups };
}
