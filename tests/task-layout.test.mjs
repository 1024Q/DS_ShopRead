import assert from 'node:assert/strict';
import test from 'node:test';
import {
  confirmedLayoutPatch,
  draftStepParent,
  editorGroups,
  editorSteps,
  effectiveStepMode,
  formatCadence,
  formatDuration,
  layoutPendingPatch,
  newTaskGroupId,
  stepParent,
} from '../app/lib/task-layout.mjs';
import { assignmentMatches, confirmedAssignments } from '../app/lib/module-save.mjs';

test('one historical query preserves paused ranges and projects a unified draft assignment', () => {
  const members = ['ad_reports@yesterday', 'ad_reports@week', 'ad_reports@month'];
  const c = {
    frequency_groups: [{ id: 'custom' }],
    task_step_catalog: [{ id: 'ad_reports@history_query', members }],
    task_modes: { pdd: Object.fromEntries(members.map((id, i) => [id, i ? 'off' : 'custom'])) },
    task_layout: { assignments: { pdd: Object.fromEntries(members.map((id) => [id, 'reports'])) } },
  };
  assert.equal(effectiveStepMode(c, 'pdd', 'ad_reports@history_query'), 'mixed');
  assert.equal(stepParent(c, 'pdd', 'ad_reports@history_query'), 'reports');
  const patch = { step_assignments: members.map((step_id) => ({ platform: 'pdd', step_id, group_id: 'custom_task' })) };
  assert.equal(draftStepParent(c, 'pdd', 'ad_reports@history_query', patch), 'custom_task');
  assert.equal(c.task_modes.pdd['ad_reports@week'], 'off');
});

const steps = [
  { id: 'overview@base', kind: 'overview', label: '实时概览', home: true, platforms: ['pdd', 'jd'] },
  { id: 'orders@today', kind: 'orders', label: '今日订单', home: false, platforms: ['pdd'] },
];
const config = {
  task_step_catalog: steps,
  frequency_groups: [
    { id: 'realtime', name: '实时', seconds: 60, timeout: 40 },
    { id: 'custom', name: '常规', seconds: 80, timeout: 60 },
  ],
  task_layout: {
    version: 1,
    groups: [
      { id: 'home', name: '首页经营指标' },
      { id: 'orders', name: '订单' },
      { id: 'business', name: '日常业务' },
    ],
    assignments: { pdd: { 'overview@base': 'home', 'orders@today': 'orders' }, jd: { 'overview@base': 'home' } },
  },
  task_modes: { pdd: { 'overview@base': 'realtime' } },
};

test('atomic frequency preserves explicit pause, independent cadence and home-only fallback', () => {
  const value = structuredClone(config);
  value.home_only_pause_revision = '20261015-home-only-1';
  assert.equal(effectiveStepMode(value, 'pdd', steps[0]), 'realtime');
  assert.equal(effectiveStepMode(value, 'pdd', steps[1]), 'off');
  value.task_modes.pdd['orders@today'] = 'custom';
  assert.equal(effectiveStepMode(value, 'pdd', steps[1]), 'custom');
  value.task_modes.pdd['overview@base'] = 'off';
  assert.equal(effectiveStepMode(value, 'pdd', steps[0]), 'off');
  assert.equal(effectiveStepMode(value, 'jd', 'overview@base'), 'custom');
  value.platform_modes = { jd: { overview: 'realtime' } };
  assert.equal(effectiveStepMode(value, 'jd', 'overview@base'), 'realtime');
  assert.equal(stepParent(value, 'pdd', steps[0].id), 'home');
});

test('node editor unions definitions and supported steps without hiding per-node differences', () => {
  const second = structuredClone(config);
  second.task_layout.groups[0].name = '本机首页';
  second.task_layout.groups = second.task_layout.groups.filter((group) => group.id !== 'business');
  second.task_step_catalog.push({ id: 'tax@base', kind: 'tax', label: '报税', platforms: ['jd'] });
  const nodes = [{ collection: config }, { collection: second }];
  assert.deepEqual(
    editorGroups({}, nodes).find((group) => group.id === 'home'),
    {
      id: 'home',
      name: '首页经营指标',
      mixed: true,
      missing: false,
    },
  );
  assert.equal(editorGroups({}, nodes).find((group) => group.id === 'business').missing, true);
  assert.deepEqual(
    editorSteps({}, nodes, 'pdd').map((step) => step.id),
    ['overview@base', 'orders@today'],
  );
  assert.deepEqual(
    editorSteps({}, nodes, 'jd').map((step) => step.id),
    ['overview@base', 'tax@base'],
  );
  assert.equal(editorGroups(config, [{ collection: config }, {}])[0].missing, true);
});

test('unchanged fields are omitted while differences remain explicit patches', () => {
  const second = structuredClone(config);
  second.task_layout.groups[0].name = '另一台名称';
  const patch = {
    group_updates: { home: { name: '首页经营指标' }, orders: { name: '订单' }, business: null },
    step_assignments: [
      { platform: 'pdd', step_id: 'overview@base', group_id: 'home' },
      { platform: 'pdd', step_id: 'orders@today', group_id: 'home' },
    ],
    group_moves: { business: 'home' },
  };
  assert.deepEqual(layoutPendingPatch(patch, { node_configs: [{ collection: config }, { collection: second }] }), {
    group_updates: { home: { name: '首页经营指标' }, business: null },
    step_assignments: [{ platform: 'pdd', step_id: 'orders@today', group_id: 'home' }],
    group_moves: { business: 'home' },
  });
  assert.equal(config.task_layout.groups[0].name, '首页经营指标');
});

test('draft deletion is projected per node before computing common ownership', () => {
  const second = structuredClone(config);
  second.task_layout.assignments.pdd['overview@base'] = 'orders';
  const patch = { group_updates: { home: null }, group_moves: { home: 'orders' } };
  assert.equal(draftStepParent(config, 'pdd', 'overview@base', patch), 'orders');
  assert.equal(draftStepParent(second, 'pdd', 'overview@base', patch), 'orders');
  patch.step_assignments = [{ platform: 'pdd', step_id: 'overview@base', group_id: 'business' }];
  assert.equal(draftStepParent(config, 'pdd', 'overview@base', patch), 'business');
  assert.equal(draftStepParent(second, 'jd', 'overview@base', patch), 'orders');
});

test('task identifiers work without secure-context randomUUID', () => {
  const ids = new Set(Array.from({ length: 20 }, () => newTaskGroupId()));
  assert.equal(ids.size, 20);
  for (const id of ids) assert.match(id, /^task_[a-zA-Z0-9_-]+$/);
});

test('delete confirmation verifies transferred steps on every platform', () => {
  const before = { collection: config };
  const after = structuredClone(config);
  after.task_layout.groups = after.task_layout.groups.filter((group) => group.id !== 'home');
  after.task_layout.assignments.pdd['overview@base'] = 'orders';
  after.task_layout.assignments.jd['overview@base'] = 'orders';
  const patch = { group_updates: { home: null }, group_moves: { home: 'orders' } };
  assert.equal(confirmedLayoutPatch(patch, { collection: after }, before), true);
  assert.equal(confirmedLayoutPatch(patch, { collection: after }), false);
  after.task_layout.assignments.jd['overview@base'] = 'business';
  assert.equal(confirmedLayoutPatch(patch, { collection: after }, before), false);
  const override = { ...patch, step_assignments: [{ platform: 'jd', step_id: 'overview@base', group_id: 'business' }] };
  assert.equal(confirmedLayoutPatch(override, { collection: after }, before), true);
});

test('layout confirmation retains draft for offline, missing, partial and changed node scope', () => {
  const patch = { group_updates: { home: { name: '新名称' } } };
  const changed = structuredClone(config);
  changed.task_layout.groups[0].name = '新名称';
  const before = {
    scope_node_id: 0,
    node_configs: [
      { id: 1, collection: config },
      { id: 2, collection: config },
    ],
  };
  const latest = {
    scope_node_id: 0,
    node_configs: [
      { id: 1, collection: changed },
      { id: 2, collection: changed },
    ],
  };
  assert.equal(confirmedLayoutPatch(patch, latest, before), true);
  assert.equal(confirmedLayoutPatch(patch, { ...latest, node_configs: [latest.node_configs[0]] }, before), false);
  latest.node_configs[1].online = false;
  assert.equal(confirmedLayoutPatch(patch, latest, before), false);
  latest.node_configs[1] = { id: 2, collection: config };
  assert.equal(confirmedLayoutPatch(patch, latest, before), false);
  latest.node_configs[1] = { id: 2, error: '请求超时', collection: changed };
  assert.equal(confirmedLayoutPatch(patch, latest, before), false);
  assert.equal(confirmedLayoutPatch(patch, { scope_node_id: 0, collection: changed }), false);
});

test('atomic frequency confirmation does not use cached offline configuration', () => {
  const item = { platform: 'pdd', kind: 'overview@base', tab: 'step', target: 'realtime' };
  assert.equal(assignmentMatches(config, item), true);
  assert.equal(assignmentMatches(config, { ...item, tab: 'unknown', target: 'default' }), false);
  assert.deepEqual(confirmedAssignments([item], { node_configs: [{ collection: config }] }), [item]);
  assert.deepEqual(confirmedAssignments([item], { node_configs: [{ collection: config, online: false }] }), []);
  assert.deepEqual(confirmedAssignments([item], { node_configs: [{ collection: config, error: '超时' }] }), []);
  assert.deepEqual(confirmedAssignments([item], { scope_node_id: 0, collection: config }), []);
});

test('duration and interval labels remain readable and unknown duration is explicit', () => {
  assert.equal(formatDuration(null), '暂无实测');
  assert.equal(formatDuration(undefined), '暂无实测');
  assert.equal(formatDuration(0), '0 秒');
  assert.equal(formatDuration(80), '1 分钟 20 秒');
  assert.equal(formatDuration(3600), '1 小时');
  assert.equal(formatCadence({ seconds: 1800 }), '每 30 分钟');
  assert.equal(formatCadence({ seconds: 80 }), '每 80 秒');
  assert.equal(formatCadence({ time: '03:00', seconds: 86400 }), '每日 03:00');
});
