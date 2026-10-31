import assert from 'node:assert/strict';
import { compareTrend } from '../app/lib/performance.mjs';
const row = (series, date, hour, sales) => ({ series, date, hour, sales });
const input = [
  row('今日累计', '2026-10-09', 0, 10),
  row('今日累计', '2026-10-09', 2, 30),
  row('昨日累计', '2026-10-08', 0, 20),
  row('昨日累计', '2026-10-08', 1, 40),
  row('昨日累计', '2026-10-08', 23, 200),
];
const compare = () => compareTrend(input, 'sales', '2026-10-09', '2026-10-08');
assert.equal(compare().hour, 0);
assert.equal(compare().change, -50);
assert.equal(compare().series[0].length, 2);
input.push(row('昨日累计', '2026-10-08', 2, 0));
assert.equal(compare().change, null);
assert.equal(compare().yesterday, 0);
assert.equal(compareTrend(input, 'sales', '2026-10-10', '2026-10-09').hour, null);
assert.equal(
  compareTrend(
    [row('今日累计', '2026-10-09', 1, 0), row('昨日累计', '2026-10-08', 1, 0)],
    'sales',
    '2026-10-09',
    '2026-10-08',
  ).change,
  0,
);
console.log('PASS sparse hours, zero denominator, midnight date isolation and no future zero-fill');
import { aggregateTrend } from '../app/lib/performance.mjs';
import { compareValues, sortedRows } from '../app/lib/table-sort.mjs';
const combined = aggregateTrend(
  [
    { trend: input },
    { trend: [row('今日累计', '2026-10-09', 0, 5), row('昨日累计', '2026-10-08', 0, 10)] },
    { trend: [] },
  ],
  'sales',
  '2026-10-09',
  '2026-10-08',
);
assert.equal(combined.count, 2);
assert.equal(combined.rows.length, 6);
assert.equal(combined.rows[0].sales, 15);
assert.equal(combined.rows[0].coverage, 2);
assert.equal(combined.rows.find((r) => r.series === '今日累计' && r.hour === 2).sales, 30);
assert.equal(combined.rows.find((r) => r.series === '今日累计' && r.hour === 2).coverage, 1);
assert.equal(aggregateTrend([{ trend: input }], 'sales', '2026-10-10', '2026-10-09').count, 0);
assert(compareValues('¥1,200.20', '¥200.40', 'asc') > 0);
assert(compareValues('—', 0, 'desc') > 0);
assert(compareValues('9007199254740993', '9007199254740992', 'asc') > 0);
assert.deepEqual(
  sortedRows([{ v: null }, { v: 9 }, { v: 1 }], { field: 'v', direction: 'desc' }).map((r) => r.v),
  [9, 1, null],
);
console.log('PASS available hourly aggregates with coverage, missing hours and generic table value sorting');
