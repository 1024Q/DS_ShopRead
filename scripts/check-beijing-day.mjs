import assert from 'node:assert/strict';
import { beijingDay, nextBeijingMidnight, watchBeijingDay } from '../app/lib/beijing-day.mjs';
let clock = Date.parse('2026-12-31T15:59:59.900Z'),
  callback,
  delay;
assert.equal(beijingDay(clock), '2026-12-31');
assert.equal(nextBeijingMidnight(clock) - clock, 100);
const doc = new EventTarget(),
  win = new EventTarget(),
  changes = [];
const stop = watchBeijingDay((day) => changes.push(day), {
  now: () => clock,
  schedule: (fn, ms) => {
    callback = fn;
    delay = ms;
    return 1;
  },
  cancel: () => {},
  document: doc,
  window: win,
});
assert.equal(delay, 125);
clock += 125;
callback();
assert.deepEqual(changes, ['2027-01-01']);
win.dispatchEvent(new Event('focus'));
doc.dispatchEvent(new Event('visibilitychange'));
assert.equal(changes.length, 1);
clock += 3 * 86400000;
doc.dispatchEvent(new Event('visibilitychange'));
assert.deepEqual(changes, ['2027-01-01', '2027-01-04']);
stop();
clock += 86400000;
win.dispatchEvent(new Event('focus'));
callback();
assert.equal(changes.length, 2);
console.log('PASS Beijing midnight, year rollover, suspended-tab catchup, deduplication and cleanup');
