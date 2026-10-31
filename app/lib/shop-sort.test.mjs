import test from 'node:test';
import assert from 'node:assert/strict';
import { sortShops } from './shop-sort.mjs';

const shops = [
  { id: 1, name: '乙店', platform: 'taobao', state: 'logged_in', operator: '张三' },
  { id: 2, name: '甲店', platform: 'pdd', state: 'stopped' },
  { id: 3, name: '丙店', platform: 'pdd', state: 'needs_verification', operators: ['李四'] },
  { id: 4, name: '丁店', platform: 'pdd', state: 'logged_in', operator: '张三' },
];
const ids = (list) => list.map((s) => s.id);

test('attention puts shops that need a person first, then platform and name', () => {
  assert.deepEqual(ids(sortShops(shops, 'attention')), [3, 4, 1, 2]);
});
test('other modes', () => {
  assert.deepEqual(ids(sortShops(shops, 'added')), [1, 2, 3, 4]);
  assert.equal(sortShops(shops, 'platform')[3].platform, 'taobao');
  assert.equal(sortShops(shops, 'operator').at(-1).id, 2);
});
test('sorting never mutates the source list', () => {
  sortShops(shops, 'name');
  assert.deepEqual(ids(shops), [1, 2, 3, 4]);
});
