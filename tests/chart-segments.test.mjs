import test from 'node:test';
import assert from 'node:assert/strict';
import { chartSegments } from '../app/lib/chart-segments.mjs';
test('missing observations and omitted hours break the line, while zero remains measured', () => {
  const rows = [
    { hour: 0, v: 0 },
    { hour: 1, v: 2 },
    { hour: 2, v: null },
    { hour: 3, v: 4 },
    { hour: 6, v: 5 },
  ];
  assert.deepEqual(
    chartSegments(rows, 'v', (r) => `${r.hour},${r.v}`),
    ['0,0 1,2', '3,4', '6,5'],
  );
});
test('daily series do not connect across a missing date', () => {
  const rows = [
    { date: '2026-10-01', v: 1 },
    { date: '2026-10-03', v: 3 },
  ];
  assert.deepEqual(
    chartSegments(rows, 'v', (r) => String(r.v)),
    ['1', '3'],
  );
});
