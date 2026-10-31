import test from 'node:test';
import assert from 'node:assert/strict';
import { formatInterval, groupSourceRows } from '../app/lib/source-display.mjs';
import { filterSources } from '../app/lib/source-filter.mjs';
import { retrySourceGroup } from '../app/lib/source-retry.mjs';
test('interval labels avoid repeating decimals', () => {
  assert.equal(formatInterval(80), '80 秒');
  assert.equal(formatInterval(60), '1 分钟');
  assert.equal(formatInterval(300), '5 分钟');
  assert.equal(formatInterval(3600), '1 小时');
});
test('same shop and module remain separate across frequency queues and filters', () => {
  const rows = ['custom', 'realtime', 'custom2'].map((queue) => ({
    shop_id: 1,
    kind: 'orders',
    queue,
    queues: [queue],
    data_status: queue === 'realtime' ? 'running' : 'queued',
  }));
  const groups = groupSourceRows(rows, [
    { id: 'realtime', name: '实时' },
    { id: 'custom', name: '常规' },
    { id: 'custom2', name: '慢速' },
  ]);
  assert.deepEqual(
    groups.map((g) => [g.name, g.rows.length]),
    [
      ['实时', 1],
      ['常规', 1],
      ['慢速', 1],
    ],
  );
  const filtered = filterSources({ rows }, { cadence: 'custom2' });
  assert.equal(filtered.total, 1);
  assert.equal(filtered.rows[0].queue, 'custom2');
});
test('retry targets only the selected frequency queue', async () => {
  const requests = [];
  const selected = { shop_id: 1, kind: 'orders', queue: 'custom2', cache_key: 'orders:slow', data_status: 'error' };
  await retrySourceGroup(selected, async (url, options) => {
    requests.push([url, options?.method]);
    return {
      ok: true,
      json: async () =>
        options?.method === 'POST'
          ? {}
          : { rows: [{ ...selected, queue: 'realtime', cache_key: 'orders:today' }, selected] },
    };
  });
  assert.match(requests[0][0], /cadence=custom2/);
  assert.match(requests[1][0], /cache_key=orders%3Aslow/);
  assert.equal(requests.length, 2);
});
