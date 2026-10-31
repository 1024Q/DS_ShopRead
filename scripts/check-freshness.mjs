import assert from 'node:assert/strict';
import { freshness } from '../app/lib/freshness.mjs';
const now = Date.now();
assert.equal(freshness({}, now).tone, 'missing');
assert.equal(freshness({ updated_at: new Date(now).toISOString() }, now).label, '刚刚更新');
assert.equal(freshness({ updated_at: (now - 5 * 60000) / 1000, stale: true }, now).label, '5 分钟前 · 待更新');
assert.equal(freshness({ updated_at: now / 1000, complete: false }, now).tone, 'partial');
console.log('PASS: missing, numeric/ISO timestamps, stale and partial freshness labels');
