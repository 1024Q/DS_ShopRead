import assert from 'node:assert/strict';
import { shopLabel } from '../app/lib/shop-identity.mjs';
assert.equal(shopLabel({ id: 1, name: '同名店', platform: 'pdd' }), '拼多多 · 同名店');
assert.equal(shopLabel({ id: 2, name: '同名店', platform: 'taobao' }), '淘宝 · 同名店');
assert.equal(shopLabel({ shop_id: 3, shop_name: '同名店', platform: 'jd' }), '京东 · 同名店');
assert.equal(shopLabel({ shop_id: 8, shop_name: '历史店' }), '平台未知 · 历史店');
assert.equal(shopLabel({ id: 4, name: '', platform: 'douyin' }), '抖店 · 店铺 4');
assert.equal(shopLabel({ shop_label: '拼多多 · 同名店、京东 · 同名店' }), '拼多多 · 同名店、京东 · 同名店');
console.log('PASS shop identity across platforms, blank names and unknown legacy records');
