import { refundSummary } from './refund-summary.mjs';
import { reportSpend } from './overview-ads.mjs';

// Keep goods-range statistics separate from today's platform-home statistics.
// Missing values stay missing; neither source may stand in for the other.
export function mergeShopOperations(shops, goods, overview, trades, ads, orderRefunds, today) {
  const byShop = (rows, id) => rows?.find((row) => String(row.shop_id) === String(id));
  const refunds = refundSummary(trades, shops, '', today, orderRefunds);
  return shops.map((shop) => {
    const product = byShop(goods?.metrics, shop.id);
    return {
      ...byShop(overview?.sources, shop.id),
      ...byShop(overview?.metrics, shop.id),
      ...byShop(refunds.rows, shop.id),
      shop_id: shop.id,
      shop_name: shop.name,
      platform: shop.platform || 'pdd',
      ad_spend: reportSpend(ads?.metrics || [], shop),
      goods_summary: product?.summary || {},
      goods_available: !!product,
      goods_source_time: product?.source_time,
    };
  });
}

export function operationsCsv(headers, rows) {
  const cell = (value) => {
    let text = String(value ?? '');
    if (typeof value === 'string' && /^[=+\-@\t\r]/.test(text)) text = "'" + text;
    return `"${text.replaceAll('"', '""')}"`;
  };
  return '\ufeff' + [headers, ...rows].map((row) => row.map(cell).join(',')).join('\r\n');
}
