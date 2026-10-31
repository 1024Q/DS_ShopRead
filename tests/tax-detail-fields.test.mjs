import test from 'node:test';
import assert from 'node:assert/strict';
import { detailFields } from '../app/lib/tax-detail-fields.mjs';
test('JD details exclude Xiaohongshu labels and retain JD amounts', () => {
  const fields = detailFields({ platform: 'jd', commission_base: null });
  assert.ok(fields.every(([, label]) => !label.includes('小红书')));
  assert.ok(fields.some(([key]) => key === 'ads_union'));
  assert.ok(fields.some(([key]) => key === 'goods_net'));
});
test('Tmall and Xiaohongshu details exclude other platform fields', () => {
  assert.ok(detailFields({ platform: 'taobao' }).every(([key]) => !['commission_base', 'ads_union'].includes(key)));
  const fields = detailFields({ platform: 'xiaohongshu' }).map(([key]) => key);
  assert.ok(fields.includes('commission_base'));
  assert.ok(fields.includes('settlement_net'));
  assert.ok(!fields.includes('ads_union'));
  assert.ok(!fields.includes('service_net'));
});
