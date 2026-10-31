import assert from 'node:assert/strict';
import { settingsPatch } from '../app/lib/settings-patch.mjs';
import { assignmentMatches, confirmedAssignments } from '../app/lib/module-save.mjs';
const before = {
  frequency_groups: [
    { id: 'a', name: '原名', seconds: 60, timeout: 120 },
    { id: 'b', name: '备用', seconds: 300, timeout: 240 },
  ],
};
const after = structuredClone(before);
after.frequency_groups[0].timeout = 180;
assert.deepEqual(settingsPatch(before, after), { group_updates: { a: { timeout: 180 } }, group_moves: {} });
const removed = { ...before, frequency_groups: [before.frequency_groups[1]], group_moves: { a: 'b' } };
assert.deepEqual(settingsPatch(before, removed), { group_updates: { a: null }, group_moves: { a: 'b' } });
const item = { tab: 'task', platform: 'pdd', kind: 'orders@unpaid', target: 'a' };
const c = { task_modes: { pdd: { 'orders@unpaid': 'a' } } };
assert.equal(assignmentMatches(c, item), true);
assert.deepEqual(confirmedAssignments([item], { node_configs: [{ collection: c }, { collection: {} }] }), []);
console.log('4 frequency UI checks passed');
