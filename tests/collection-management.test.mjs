import test from 'node:test';
import assert from 'node:assert/strict';
import {
  stepState,
  shopRows,
  uploadSummary,
  orderedIds,
  csvCell,
  executionLogRows,
} from '../app/lib/collection-management.mjs';
test('recovered historical errors do not mark the current shop abnormal', () => {
  assert.equal(stepState({ phase: 'waiting', failures: 3, last_attempt: 100, last_success: 101 }), 'waiting');
  assert.equal(stepState({ phase: 'running', failures: 3 }), 'running');
  assert.equal(stepState({ phase: 'waiting', failures: 1, last_attempt: 102, last_success: 101 }), 'issue');
});
test('one row per shop and no duplicate counting of grouped ranges', () => {
  const rows = shopRows(
    [
      { id: 1, name: 'A', state: 'sleeping' },
      { id: 2, name: 'B', state: 'stopped' },
    ],
    [
      {
        shop_id: 1,
        steps: [
          {
            phase: 'running',
            range_steps: [
              { name: '今日', phase: 'running' },
              { name: '昨日', phase: 'waiting' },
            ],
          },
        ],
      },
    ],
  );
  assert.equal(rows.length, 2);
  assert.equal(rows[0].runningCount, 1);
  assert.equal(rows[0].steps.length, 2);
  assert.match(rows[1].waitingReason, /停止/);
});
test('upload summary keeps oldest backlog and concrete failure until confirmed', () => {
  const result = uploadSummary([
    { last_timing: { finished_at: 10 }, upload_timing: { state: 'pending' } },
    { last_timing: { finished_at: 20 }, upload_timing: { state: 'failed', reason: '断线' } },
    { upload_timing: { state: 'confirmed' } },
  ]);
  assert.deepEqual(result, { state: 'failed', pending: 2, confirmed: 1, oldest: 10, error: '断线' });
});
test('sorting is explicit and spreadsheet export neutralizes formulas', () => {
  assert.deepEqual(
    orderedIds(
      [
        { key: 1, name: 'B' },
        { key: 2, name: 'A' },
      ],
      'name',
    ),
    [2, 1],
  );
  assert.equal(csvCell('=1+1'), '"\'=1+1"');
  assert.equal(csvCell('a"b'), '"a""b"');
});

test('file history uses actual file timestamps and combines repeated errors only', () => {
  const rows = executionLogRows([
    { id: 3, created: 30, last_seen: 30, occurrences: 1, level: 'error', module: 'orders', message: '超时' },
    { id: 2, created: 20, last_seen: 20, occurrences: 1, level: 'info', module: 'orders', message: '成功' },
    { id: 1, created: 10, last_seen: 10, occurrences: 1, level: 'error', module: 'orders', message: '超时' },
  ]);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].first_seen, 10);
  assert.equal(rows[0].last_seen, 30);
  assert.equal(rows[0].count, 2);
});
