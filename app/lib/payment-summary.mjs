import { inScope } from './filter-scope.mjs';
const valid = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0;
export function paymentSummary(data, shops, shopId, today) {
  const scope = shops.filter((s) => inScope(shopId, s.id));
  const current = data?.scope?.day === today && data?.scope?.trade_range === 'realtime';
  const rows = scope.map((shop) => {
    const metric = current ? data?.metrics?.find((m) => String(m.shop_id) === String(shop.id)) : null;
    const source = current ? data?.sources?.find((m) => String(m.shop_id) === String(shop.id)) : null;
    return {
      shop_id: shop.id,
      source,
      ...Object.fromEntries(
        ['sales', 'orders', 'buyers'].map((key) => [
          'paid_' + key,
          valid(metric?.summary?.[key]) ? metric.summary[key] : null,
        ]),
      ),
    };
  });
  const totals = {},
    coverage = {};
  for (const key of ['paid_sales', 'paid_orders', 'paid_buyers']) {
    const values = rows.map((r) => r[key]).filter(valid);
    coverage[key] = values.length;
    totals[key] = values.length
      ? key === 'paid_sales'
        ? values.reduce((sum, v) => sum + Math.round(v * 100), 0) / 100
        : values.reduce((sum, v) => sum + v, 0)
      : null;
  }
  return { rows, totals, coverage, count: scope.length, current };
}
