import test from 'node:test';
import assert from 'node:assert/strict';
import { throttleUpdates, updateMatches } from './data-updates.mjs';

test('bursts of updates run once immediately and once trailing', () => {
  let clock = 0,
    runs = 0,
    pending = null;
  const handler = throttleUpdates(
    () => runs++,
    3000,
    () => clock,
    (fn, ms) => (pending = { fn, at: clock + ms }),
    () => (pending = null),
  );
  handler();
  assert.equal(runs, 1);
  for (let i = 0; i < 10; i++) {
    clock += 100;
    handler();
  }
  assert.equal(runs, 1);
  assert.equal(pending.at, 3000);
  clock = pending.at;
  pending.fn();
  assert.equal(runs, 2);
  clock += 5000;
  handler();
  assert.equal(runs, 3);
  handler();
  handler.cancel();
  assert.equal(pending, null);
});

test('updates for other shops are ignored', () => {
  const detail = { changes: [{ shop_id: 2, kind: 'orders' }] };
  assert.equal(updateMatches(detail, { shopId: '1' }), false);
  assert.equal(updateMatches(detail, { shopId: '' }), true);
  assert.equal(updateMatches({ reset: true }, { shopId: '1' }), true);
});
