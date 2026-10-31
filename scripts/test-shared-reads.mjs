import assert from 'node:assert/strict';
import { sharedRead } from '../app/lib/shared-read.mjs';
import { setSnapshotScope } from '../app/lib/response-snapshots.mjs';
import { databaseReader } from '../app/lib/database-reader.js';
setSnapshotScope('fixture');
let calls = 0;
globalThis.fetch = async () => {
  calls++;
  await new Promise((r) => setTimeout(r, 10));
  return new Response(JSON.stringify({ value: calls, sources: [] }));
};
const a = new AbortController();
const first = sharedRead('/api/data/overview?a=1&b=2', { signal: a.signal });
const second = sharedRead('/api/data/overview?b=2&a=1');
a.abort();
await assert.rejects(first, { name: 'AbortError' });
assert.equal((await (await second).json()).value, 1);
assert.equal(calls, 1);
await sharedRead('/api/data/overview?a=1&b=2');
assert.equal(calls, 2); // No stale TTL after a published update.
setSnapshotScope('other-machine');
await sharedRead('/api/data/overview?a=1&b=2');
assert.equal(calls, 3);
const commands = [];
globalThis.fetch = async (url, options) => {
  commands.push({ url, method: options?.method || 'GET' });
  return new Response(JSON.stringify({ sources: [{ shop_id: 1, updated_at: null }], tasks: [{}] }));
};
const read = databaseReader('/api/data/overview');
await read(new URLSearchParams('shop_id=1'), { registerMissing: false });
assert.equal(commands.filter((c) => c.method === 'POST').length, 0);
await Promise.all([
  read(new URLSearchParams('shop_id=1'), { force: true }),
  read(new URLSearchParams('shop_id=1'), { force: true }),
]);
assert.equal(commands.filter((c) => c.method === 'POST').length, 1);
const manual = new URL(commands.find((c) => c.method === 'POST').url, 'http://localhost');
assert.equal(manual.searchParams.get('force'), 'true');
assert.notEqual(manual.searchParams.get('source'), 'automatic');
await read(new URLSearchParams('shop_id=2'));
const automatic = commands.filter(
  (c) => c.method === 'POST' && new URL(c.url, 'http://localhost').searchParams.get('shop_id') === '2',
);
assert.equal(automatic.length, 1);
const background = new URL(automatic[0].url, 'http://localhost');
assert.equal(background.searchParams.get('source'), 'automatic');
assert.equal(background.searchParams.get('force'), null);
console.log(
  'PASS: coalesced reads, abort isolation, no stale reads, machine isolation, read-only refresh, manual collection deduplication, automatic registration intent',
);
