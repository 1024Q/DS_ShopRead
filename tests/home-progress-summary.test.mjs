import assert from 'node:assert/strict';
import { test } from 'node:test';
import { overdueBreakdown } from '../app/lib/home-progress-summary.mjs';

test('overdue breakdown retains running/waking and excludes daily/paused entries', () => {
  const item = { included: true, overdue: true, group: 'core' };
  const rows = [
    {
      modules: [
        { ...item, status: 'running' },
        { ...item, status: 'waking', overdue_reason: 'waking' },
        { ...item, status: 'queued' },
        { ...item, status: 'running', overdue_reason: 'saving' },
        { ...item, status: 'queued', included: false },
        { ...item, status: 'queued', group: 'daily' },
        { ...item, status: 'updated', overdue: false },
      ],
    },
  ];
  assert.deepEqual(Object.fromEntries(overdueBreakdown(rows).map((r) => [r.key, r.count])), {
    queued: 1,
    waking: 1,
    running: 1,
    saving: 1,
  });
});
