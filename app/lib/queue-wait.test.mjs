import test from 'node:test';
import assert from 'node:assert/strict';
import { waitText } from './queue-wait.mjs';

test('wait estimate text', () => {
  assert.equal(waitText(null), '预计开始时间暂无法估算');
  assert.equal(waitText(2), '即将开始');
  assert.equal(waitText(42), '约 42 秒后开始');
  assert.equal(waitText(300), '约 5 分钟后开始');
  assert.equal(waitText(3 * 3600 + 600), '约 3 小时 10 分钟后开始');
  const t = (s, v) => (s === '约 {n} 秒后开始' ? `in about ${v.n} s` : s);
  assert.equal(waitText(42, t), 'in about 42 s');
});
