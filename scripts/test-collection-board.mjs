import assert from 'node:assert/strict';
import { assignModule, selectedMode, frequencyLabel } from '../app/lib/collection-board.mjs';
const initial = {
  module_modes: { packages: 'custom', orders: 'daily' },
  platform_modes: { pdd: { reviews: 'custom' }, taobao: { products: 'off' } },
  frequency_names: { custom: '物流巡检' },
  custom_seconds: 300,
  daily_time: '06:00',
};
assert.equal(selectedMode(initial, 'pdd', 'packages'), 'custom');
const changed = assignModule(initial, 'pdd', 'packages', 'daily');
assert.equal(selectedMode(changed, 'pdd', 'packages'), 'daily');
assert.equal(selectedMode(changed, 'pdd', 'orders'), 'daily');
assert.equal(selectedMode(changed, 'taobao', 'products'), 'off');
assert.equal(selectedMode(initial, 'pdd', 'packages'), 'custom');
assert.equal(selectedMode(assignModule(changed, 'pdd', 'packages', 'default'), 'pdd', 'packages'), undefined);
assert.equal(frequencyLabel(initial, 'custom'), '物流巡检 · 每 5 分钟');
for (const mode of ['custom2', 'custom3']) {
  const config = {
    ...initial,
    [mode + '_seconds']: 660,
    frequency_names: { ...initial.frequency_names, [mode]: '独立巡检' },
  };
  assert.equal(frequencyLabel(config, mode), '独立巡检 · 每 11 分钟');
  assert.equal(selectedMode(assignModule(config, 'taobao', 'products', mode), 'taobao', 'products'), mode);
  assert.equal(selectedMode(assignModule(config, 'taobao', 'products', mode), 'pdd', 'packages'), 'custom');
}
console.log('collection board: pass');

const { selectedPriority } = await import('../app/lib/collection-board.mjs');
const { confirmedAssignments } = await import('../app/lib/module-save.mjs');
assert.equal(selectedPriority({}, 'pdd', 'overview'), 'high');
assert.equal(selectedPriority({}, 'pdd', 'products'), 'high');
assert.equal(selectedPriority({}, 'pdd', 'orders'), 'medium');
assert.equal(selectedPriority({ platform_priorities: { pdd: { overview: 'low' } } }, 'pdd', 'overview'), 'low');
const assignment = { platform: 'pdd', kind: 'orders', tab: 'priority', target: 'high' };
const collection = { platform_priorities: { pdd: { orders: 'high' } } };
assert.equal(confirmedAssignments([assignment], { node_configs: [{ collection }, { collection: {} }] }).length, 0);
assert.equal(confirmedAssignments([assignment], { node_configs: [{ collection }, { collection }] }).length, 1);
console.log('priority defaults and per-node save confirmation: pass');

const { commonValue, changedValue } = await import('../app/lib/node-policy.mjs');
const nodes = [
  { collection: { shop_concurrent: 2, max_concurrent: 8 } },
  { collection: { shop_concurrent: 2, max_concurrent: 6 } },
];
assert.equal(
  commonValue(nodes, (c) => c.max_concurrent),
  undefined,
);
assert.equal(
  commonValue(nodes, (c) => c.shop_concurrent),
  2,
);
assert.equal(
  commonValue([...nodes, { error: 'offline' }], (c) => c.shop_concurrent),
  undefined,
);
assert.deepEqual(changedValue(nodes, 'shop_concurrent', 3, {}), { shop_concurrent: 3 });
assert.deepEqual(changedValue(nodes, 'shop_concurrent', 2, { shop_concurrent: 3 }), {});
let request;
globalThis.fetch = async (url, options) => {
  request = { url, options };
  return { ok: true, json: async () => ({ ok: true }) };
};
const { settingsRequest } = await import('../app/lib/module-save.mjs');
await settingsRequest('PUT', { shop_concurrent: 3 }, 1000, 0, 'system/concurrency');
assert.equal(request.options.headers['x-ecmp-config-node'], '0');
assert.equal(request.options.body, JSON.stringify({ shop_concurrent: 3 }));
await settingsRequest('GET', undefined, 1000, 3);
assert.equal(request.options.headers['x-ecmp-config-node'], '3');
console.log('mixed configuration, delta patch and bound machine scope: pass');
