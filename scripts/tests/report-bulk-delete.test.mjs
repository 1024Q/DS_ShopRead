import test from 'node:test';
import assert from 'node:assert/strict';
import { deleteSelectedReports, retainReportSelection } from '../../app/lib/report-delete.mjs';

test('only selected reports are deleted, duplicates coalesce, failures do not stop the batch', async () => {
  const selected = [{ id: 'a' }, { id: 'b' }, { id: 'a' }, { id: 'c' }, { id: 'd' }];
  const calls = [],
    progress = [];
  let active = 0,
    peak = 0;
  const results = await deleteSelectedReports(
    selected,
    async (job) => {
      calls.push(job.id);
      peak = Math.max(peak, ++active);
      try {
        await new Promise((resolve) => setTimeout(resolve, job.id === 'a' ? 15 : 2));
        if (job.id === 'b') throw Error('文件正在占用');
        if (job.id === 'c') {
          const error = Error('不存在');
          error.status = 404;
          throw error;
        }
        return { node_cleanup_pending: job.id === 'd' };
      } finally {
        active--;
      }
    },
    (result, done, total) => progress.push({ id: result.id, done, total }),
  );
  assert.deepEqual(calls.toSorted(), ['a', 'b', 'c', 'd']);
  assert.equal(peak, 2);
  assert.equal(results.length, 4);
  assert.deepEqual(
    results.filter((r) => !r.ok).map((r) => r.id),
    ['b'],
  );
  assert.equal(results.find((r) => r.id === 'c').alreadyDeleted, true);
  assert.equal(results.find((r) => r.id === 'd').pending, true);
  assert.deepEqual(
    progress.map((r) => r.done),
    [1, 2, 3, 4],
  );
  assert.ok(progress.every((r) => r.total === 4));
  assert.equal(selected.length, 5);
});

test('refresh drops missing selections and never selects new tasks automatically', () => {
  assert.deepEqual(retainReportSelection(['a', 'b', 'b'], [{ id: 'b' }, { id: 'c' }]), ['b']);
  assert.deepEqual(retainReportSelection([], [{ id: 'new' }]), []);
});

test('empty selection does not issue deletion requests', async () => {
  const result = await deleteSelectedReports([], () => assert.fail('unexpected deletion'));
  assert.deepEqual(result, []);
});
