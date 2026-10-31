// Keep homepage spend on the same report scope as the promotion metrics.
export function reportSpend(metrics, shop) {
  const row = metrics.find((m) => String(m.shop_id) === String(shop.id));
  const value = (shop.platform || 'pdd') === 'pdd' ? row?.total_spend : row?.ad_spend;
  return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null;
}
