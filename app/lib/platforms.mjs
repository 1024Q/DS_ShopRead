import { inScope } from './filter-scope.mjs';
import registry from '../../config/platforms.json' with { type: 'json' };
export const platformRegistry = registry;
export const platforms = Object.fromEntries(Object.entries(registry).map(([id, p]) => [id, p.name]));
export const systemGroups = [
  [
    '系统管理',
    [
      ['cost_tables', '成本表'],
      ['fleet', '跨机器汇总'],
      ['shops', '店铺管理'],
      ['browser_workspace', '店铺浏览器'],
      ['sources', '同步与数据源'],
      ['machines', '远程机器'],
      ['settings', '系统设置'],
      ['logs', '运行日志'],
      ['accounts', '账号与权限'],
    ],
  ],
];
export const systemViews = new Set(systemGroups[0][1].map(([id]) => id));
export function platformGroups(platform) {
  return registry[platform]?.groups || [];
}
export const exclusivePlatforms = Object.fromEntries(
  Object.entries(registry).flatMap(([platform, config]) =>
    (config.exclusiveViews || []).map((view) => [view, platform]),
  ),
);
export const commonGroups = registry.pdd.groups
  .map(([name, items]) => [name, items.filter(([id]) => !exclusivePlatforms[id])])
  .filter(([, items]) => items.length);
export const accountingGroups = [
  [
    '对账中心',
    [
      ['report_exports', '报表导出'],
      ['report_view', '详细报表'],
      ['promotion_ledger', '多多推广流水'],
      ['profit', '利润核算'],
      ['profit_tax', '税费预估与核对'],
    ],
  ],
];
export const exclusiveGroups = Object.entries(registry)
  .filter(([, config]) => config.exclusiveViews?.length)
  .map(([id, config]) => [
    config.name + '专属',
    config.groups
      .flatMap(([, items]) => items)
      .filter(([view]) => exclusivePlatforms[view] === id && !['report_exports', 'report_view'].includes(view)),
  ]);
const defaultNavigation = [...commonGroups, ...exclusiveGroups, ...accountingGroups, ...systemGroups];
// Sidebar order by daily use: data → order work → goods → marketing → money → platform tools →
// accounting → shops/machines → system. Only presentation: view ids and permissions are unchanged.
// A third element `{ standalone: true }` renders the entry as a single top-level link (no sub-menu).
const navigationLayout = [
  ['实时预览', ['overview'], { standalone: true }],
  ['重点处理', ['todos'], { standalone: true }],
  ['数据中心', ['transactions', 'goodsdata']],
  ['订单售后', ['orders', 'aftersales', 'tickets', 'packages', 'intercepts', 'evidence', 'invoices']],
  ['商品管理', ['products', 'reviews']],
  ['推广营销', ['ad_products', 'ad_reports', 'ad_logs', 'activities']],
  ['资金报税', ['reconciliation_day', 'tax']],
  ...exclusiveGroups.map(([label, items]) => [label, items.map(([id]) => id)]),
  ['对账中心', ['report_exports', 'report_view', 'cost_tables', 'promotion_ledger', 'profit', 'profit_tax']],
  ['店铺与机器', ['shops', 'browser_workspace', 'machines', 'fleet']],
  ['系统管理', ['sources', 'logs', 'settings', 'accounts']],
];
export function arrangeNavigation(groups, layout) {
  const names = new Map(groups.flatMap(([, items]) => items));
  const placed = new Set();
  const arranged = layout
    .map(([label, ids, meta]) => {
      const items = ids
        .filter((id) => names.has(id) && !placed.has(id))
        .map((id) => (placed.add(id), [id, names.get(id)]));
      return meta ? [label, items, meta] : [label, items];
    })
    .filter(([, items]) => items.length);
  // Never lose an entry that a future platform config adds but the layout does not list yet.
  const rest = [...names].filter(([id]) => !placed.has(id));
  return rest.length ? [...arranged, ['其他功能', rest]] : arranged;
}
export const navigationGroups = arrangeNavigation(defaultNavigation, navigationLayout);
export function supportsView(platform, view) {
  const key = view === 'reconciliation_month' ? 'reconciliation_day' : view;
  return (
    view === 'profit' ||
    view === 'promotion_ledger' ||
    systemViews.has(view) ||
    commonGroups.some(([, items]) => items.some(([id]) => id === key)) ||
    exclusivePlatforms[key] === platform
  );
}
export function hasBusinessAdapter(platform, kind = 'overview') {
  if (kind === 'todos')
    return ['aftersales', 'evidence', 'tickets', 'intercepts'].some((k) => hasBusinessAdapter(platform, k));
  return registry[platform]?.businessAdapter === 'pdd' || !!registry[platform]?.businessKinds?.includes(kind);
}
export function canReadShop(shop) {
  return !!shop && hasBusinessAdapter(shop.platform || 'pdd');
}
export function canReadKind(shop, kind) {
  return !!shop && hasBusinessAdapter(shop.platform || 'pdd', kind);
}
export function canReadScope(shops, shopId, kind = 'overview') {
  return shops.some((s) => inScope(shopId, s.id) && canReadKind(s, kind));
}
