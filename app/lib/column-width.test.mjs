import test from 'node:test';
import assert from 'node:assert/strict';
import { columnWidth } from './column-width.mjs';
import { operationHeaders } from './operation-headers.mjs';

test('text columns are wider than numbers; first and action columns keep fixed room', () => {
  assert.equal(columnWidth('店铺', 0), 210);
  assert.equal(columnWidth('操作', 8), 140);
  assert.ok(columnWidth('商品 / 订单', 1) > columnWidth('数量', 4));
  assert.ok(columnWidth('下单时间', 5) > columnWidth('实收金额', 3));
  assert.equal(columnWidth('', 3), 150);
  assert.equal(columnWidth('订单状态', 2), 130);
  assert.equal(columnWidth('订单数（实时口径）', 3), 120);
  assert.equal(columnWidth('退款订单数', 4), 120);
});

test('every operation list header resolves to a sensible width', () => {
  for (const headers of Object.values(operationHeaders))
    headers.forEach((label, index) => {
      const width = columnWidth(label, index);
      assert.ok(width >= 96 && width <= 260, `${label}: ${width}`);
    });
});
