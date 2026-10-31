import { platforms } from './platforms.mjs';
// A display label never replaces the stored name or the ID used for actions.
export function shopLabel(shop) {
  if (!shop) return '平台未知 · 未知店铺';
  if (shop.shop_label) return shop.shop_label;
  const id = shop.shop_id ?? shop.id,
    name = shop.shop_name || shop.name || (id != null ? `店铺 ${id}` : '未知店铺');
  const platform = shop.platform || shop.shop_platform;
  return `${platforms[platform] || '平台未知'} · ${name}`;
}
