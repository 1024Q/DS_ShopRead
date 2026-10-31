import test from 'node:test';
import assert from 'node:assert/strict';
import { compile, translateMessage, translateText } from './translate.mjs';
import en from './en.mjs';
import zhTW from './zh-TW.mjs';

const dict = compile({
  exact: { 采集队列: 'Collection queue', 排队中: 'Queued', 个: '' },
  patterns: { '共 {0} 项': '{0} items', '队列第 {n} 位': 'Queue position {n}', '{0}专属': '{0} only', 拼多多: 'x' },
});

test('exact text keeps surrounding whitespace', () => {
  assert.equal(translateText('采集队列', dict), 'Collection queue');
  assert.equal(translateText('  采集队列 ', dict), '  Collection queue ');
  assert.equal(translateText(' 个', dict), ' ');
});

test('patterns, composites and passthrough', () => {
  assert.equal(translateText('共 12 项', dict), '12 items');
  assert.equal(translateText('采集队列 · 排队中', dict), 'Collection queue · Queued');
  assert.equal(translateText('采集队列 · 未知内容', dict), 'Collection queue · 未知内容');
  assert.equal(translateText('蓝怡宝家清旗舰店', dict), '蓝怡宝家清旗舰店');
  assert.equal(translateText('English only', dict), 'English only');
  assert.equal(translateText('采集队列', null), '采集队列');
});

test('explicit messages interpolate named values', () => {
  assert.equal(translateMessage('队列第 {n} 位', { n: 3 }, null), '队列第 3 位');
  const compiled = compile({ exact: { '队列第 {n} 位': 'Queue position {n}' } });
  assert.equal(translateMessage('队列第 {n} 位', { n: 3 }, compiled), 'Queue position 3');
});

test('shipped dictionaries load and translate core labels', () => {
  const english = compile(en);
  assert.equal(translateText('采集队列', english), 'Collection queue');
  assert.equal(translateText('拼多多专属', english), 'Pinduoduo only');
  assert.equal(translateText('3 / 7 家今日已更新', english), '3 / 7 shops updated today');
  assert.equal(translateText('实时采集 · 每 1 分钟', english), 'Live collection · Every 1 min');
  const traditional = compile(zhTW);
  assert.equal(translateText('后台任务与子步骤 · 分组和频率设置', traditional), '後臺任務與子步驟 · 分組和頻率設置');
  assert.equal(translateText('采集队列', traditional), '採集隊列');
  assert.equal(translateText('实时概览-常规任务', traditional), '實時概覽-常規任務');
  assert.equal(translateText('2026-10-30', traditional), '2026-10-30');
});

test('character converter covers runtime text and explicit messages', () => {
  const tw = compile({ chars: '后後台臺队隊列列', phrases: { 皇后: '皇后' } });
  assert.equal(translateText(' 后台队列 ', tw), ' 後臺隊列 ');
  assert.equal(translateText('皇后', tw), '皇后');
  assert.equal(translateMessage('队列第 {n} 位', { n: '后' }, tw), '隊列第 后 位');
  const traditional = compile(zhTW);
  assert.equal(translateText('店铺正在后台静默采集，已运行 12 秒', traditional), '店鋪正在後臺靜默採集，已運行 12 秒');
});
