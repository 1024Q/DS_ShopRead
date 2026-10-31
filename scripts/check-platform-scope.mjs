import assert from 'node:assert/strict';
import {
  platforms,
  commonGroups,
  exclusivePlatforms,
  navigationGroups,
  supportsView,
  hasBusinessAdapter,
  systemViews,
  canReadScope,
} from '../app/lib/platforms.mjs';
assert.equal(Object.keys(platforms).length, 8);
assert(supportsView('pdd', 'reconciliation_month'));
for (const platform of Object.keys(platforms)) {
  for (const view of ['overview', 'transactions', 'ad_reports', 'products', 'reviews', 'tax', 'todos', ...systemViews])
    assert(supportsView(platform, view));
  for (const [view, owner] of Object.entries(exclusivePlatforms))
    assert.equal(supportsView(platform, view), platform === owner);
  if (!['pdd', 'taobao'].includes(platform)) assert.equal(hasBusinessAdapter(platform), false);
}
const views = navigationGroups.flatMap(([, items]) => items.map(([id]) => id));
assert.equal(new Set(views).size, views.length);
assert(!commonGroups.flatMap(([, items]) => items).some(([id]) => exclusivePlatforms[id]));
const shops = [
  { id: 1, platform: 'pdd' },
  { id: 2, platform: 'jd' },
];
assert(canReadScope(shops, ''));
assert(canReadScope(shops, '1'));
assert(!canReadScope(shops, '2'));
assert(!canReadScope([], ''));
for (const kind of ['overview', 'transactions', 'goodsdata']) assert(hasBusinessAdapter('taobao', kind));
for (const kind of ['products', 'tax', 'tickets']) assert(!hasBusinessAdapter('taobao', kind));
for (const kind of ['aftersales', 'packages', 'invoices', 'ad_products', 'ad_reports'])
  assert(hasBusinessAdapter('taobao', kind));
assert(!canReadScope([{ id: 3, platform: 'taobao' }], '3', 'products'));
console.log('PASS shared navigation, platform-specific features and unsupported shop read guards');
