import assert from 'node:assert/strict';
import { statusTone, restoreStatus, DEFAULT_STATUSES } from '../app/lib/status-tone.mjs';
import { pickupCountdown } from '../app/lib/package-countdown.mjs';
for (const kind of ['products', 'activities']) {
  assert.equal(restoreStatus(kind, { draft: { status: '' } }).status, DEFAULT_STATUSES[kind]);
  assert.equal(restoreStatus(kind, { draft: { status: '' }, statusDefaultsVersion: 1 }).status, '');
  assert.equal(restoreStatus(kind, { draft: { status: '已下架' } }).status, '已下架');
  assert.equal(restoreStatus(kind, null, { status: '' }).status, '');
}
assert.equal(statusTone('报名成功'), 'green');
assert.equal(statusTone('在售中'), 'green');
assert.notEqual(statusTone('已下架'), statusTone('在售中'));
assert.equal(new Set(['发货未揽件', '揽件未中转', '中转未派件', '派件未签收', '已签收'].map(statusTone)).size, 5);
assert.equal(statusTone('退款失败'), 'red');
assert.equal(statusTone('已撤销拦截'), 'gray');
const row = { platform: 'pdd', status: '发货未揽件', pickup_deadline: '2026-10-12T12:00:00+08:00' };
const due = Date.parse(row.pickup_deadline);
assert.equal(pickupCountdown(row, due - 2 * 3600000).text, '剩余 2.0 小时');
assert.equal(pickupCountdown(row, due + 3600000).text, '已超时 1.0 小时');
assert.equal(pickupCountdown(row, due).text, '已到揽收期限');
assert.equal(pickupCountdown({ ...row, platform: 'taobao' }, due), null);
assert.equal(pickupCountdown({ ...row, status: '已签收' }, due), null);
assert.equal(pickupCountdown({ ...row, pickup_deadline: null }, due).hours, null);
console.log('Status colors, persisted defaults and pickup countdown checks passed');
