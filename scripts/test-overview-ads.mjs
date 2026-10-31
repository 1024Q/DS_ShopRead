import assert from 'node:assert/strict';
import { reportSpend } from '../app/lib/overview-ads.mjs';
const rows = [
  { shop_id: 1, total_spend: 120, ad_spend: 100 },
  { shop_id: 2, total_spend: null, ad_spend: 30 },
];
assert.equal(reportSpend(rows, { id: 1, platform: 'pdd' }), 120);
assert.equal(reportSpend(rows, { id: '2', platform: 'taobao' }), 30);
assert.equal(reportSpend(rows, { id: 2, platform: 'pdd' }), null);
assert.equal(reportSpend(rows, { id: 3, platform: 'pdd' }), null);
assert.equal(reportSpend([{ shop_id: 1, total_spend: 0 }], { id: 1 }), 0);
console.log('Homepage promotion spend checks passed');
