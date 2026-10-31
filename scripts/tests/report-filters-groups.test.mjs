import test from 'node:test';
import assert from 'node:assert/strict';
import { shopGroupPage } from '../../app/lib/shop-group-page.mjs';
import { filterReportJobs, reportStorage, formatBytes } from '../../app/lib/report-task-tools.mjs';

test('each node paginates independently; a single shop never becomes an empty page', () => {
  const first = Array.from({ length: 26 }, (_, id) => ({ id })),
    second = [{ id: 100 }];
  assert.equal(shopGroupPage(first, 1).visible.length, 24);
  assert.equal(shopGroupPage(first, 2).visible.length, 2);
  assert.deepEqual(shopGroupPage(second, 1).visible, second);
  assert.deepEqual(shopGroupPage(second, 2).visible, second);
  assert.equal(shopGroupPage(first.slice(0, 1), 2).page, 1);
  assert.equal(shopGroupPage([], 2).visible.length, 0);
});
test('report filters combine shop, month, and exact task state', () => {
  const jobs = [
    { id: 'a', shop_id: 1, start: '2026-09-01 00:00:00', state: 'completed' },
    { id: 'b', shop_id: 1, start: '2026-10-01 00:00:00', state: 'exception' },
    { id: 'c', shop_id: 2, start: '2026-09-01 00:00:00', state: 'completed' },
  ];
  assert.deepEqual(
    filterReportJobs(jobs, { shop: '1', month: '2026-09', state: 'completed' }).map((j) => j.id),
    ['a'],
  );
  assert.equal(filterReportJobs(jobs, { state: 'exception' }).length, 1);
  assert.equal(filterReportJobs(jobs, { shop: '2', month: '2026-10' }).length, 0);
  assert.equal(filterReportJobs(jobs).length, 3);
});
test('storage totals distinguish actual file bytes, database rows, and unreported nodes', () => {
  const result = reportStorage([
    { center_file_bytes: 1024, node_file_bytes: 2048, row_count: 10 },
    { center_file_bytes: 512, row_count: 5 },
  ]);
  assert.deepEqual(result, { center: 1536, node: 2048, unknown: 1, rows: 15 });
  assert.equal(formatBytes(result.center + result.node), '3.5 KB');
  assert.equal(formatBytes(undefined), '未回报');
  assert.equal(formatBytes(0), '0 B');
});
