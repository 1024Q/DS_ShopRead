import { platformRegistry } from './platforms.mjs';

// Shops that need a person first: verification, failures and logins in progress.
const STATE_ORDER = ['needs_verification', 'login_failed', 'logging_in', 'logged_in', 'sleeping', 'stopped'];
const PLATFORM_ORDER = Object.keys(platformRegistry);
const collator = new Intl.Collator('zh-CN', { numeric: true });

export const shopSortOptions = [
  ['attention', '待处理优先'],
  ['name', '店铺名称'],
  ['platform', '平台'],
  ['operator', '运营负责人'],
  ['added', '添加顺序'],
];

const rank = (list, value) => {
  const index = list.indexOf(value);
  return index < 0 ? list.length : index;
};
const nameOf = (shop) => shop.alias || shop.detected_name || shop.name || shop.username || '';
const operatorOf = (shop) => (shop.operators?.length ? shop.operators : [shop.operator].filter(Boolean)).join('、');

export function sortShops(shops, mode = 'attention') {
  const byName = (a, b) => collator.compare(nameOf(a), nameOf(b)) || a.id - b.id;
  const byPlatform = (a, b) => rank(PLATFORM_ORDER, a.platform || 'pdd') - rank(PLATFORM_ORDER, b.platform || 'pdd');
  const compare = {
    attention: (a, b) => rank(STATE_ORDER, a.state) - rank(STATE_ORDER, b.state) || byPlatform(a, b) || byName(a, b),
    name: byName,
    platform: (a, b) => byPlatform(a, b) || byName(a, b),
    // Unassigned shops last so assigned work groups stay together.
    operator: (a, b) =>
      !operatorOf(a) - !operatorOf(b) || collator.compare(operatorOf(a), operatorOf(b)) || byName(a, b),
    added: (a, b) => a.id - b.id,
  }[mode];
  return compare ? [...shops].sort(compare) : shops;
}
