import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { appendInput, controlPath } from '../app/lib/remote-input.mjs';
const q = [];
for (const event of [
  { type: 'mousePressed', x: 1 },
  { type: 'mouseMoved', x: 2 },
  { type: 'mouseMoved', x: 5 },
  { type: 'mouseReleased', x: 5 },
  { type: 'wheel', delta_y: 100 },
  { type: 'wheel', delta_y: 200 },
  { type: 'key', key: 'Tab' },
])
  appendInput(q, event);
assert.deepEqual(
  q.map((e) => e.type),
  ['mousePressed', 'mouseMoved', 'mouseReleased', 'wheel', 'key'],
);
assert.equal(q[1].x, 5);
assert.equal(q[3].delta_y, 300);
assert.equal(controlPath('nodes/2/proxy/', 7), '/browser-control/nodes/2/shops/7');
assert.equal(controlPath('', 7), '/browser-control/shops/7');
assert.throws(() => controlPath('https://evil/', 7));
const { destination } = createRequire(import.meta.url)('../lib/remote-upgrade.cjs');
for (const req of [
  { headers: { host: 'localhost:3210', origin: 'https://evil.example' }, url: '/browser-control/shops/1' },
  { headers: { host: 'example.com', origin: 'https://example.com' }, url: '/browser-control/shops/1' },
  {
    headers: { host: 'localhost:3210', origin: 'http://localhost:3210' },
    url: '/browser-control/shops/1?target=http://evil',
  },
])
  assert.throws(() => destination(req));
console.log('PASS motion coalescing, event ordering, remote path isolation and upgrade authorization');
