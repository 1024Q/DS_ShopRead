import { hasBusinessAdapter } from './platforms.mjs';
import { formatInterval } from './source-display.mjs';

const HOME_ONLY_REVISION = '20261015-home-only-1';
export const queryMembers = (config, id) => config?.task_step_catalog?.find((s) => s.id === id)?.members || [id];
const same = (values) => (values.every((v) => v === values[0]) ? values[0] : 'mixed');

function configs(config, nodes = []) {
  return nodes.length ? nodes.map((node) => node.collection) : [config];
}

export function effectiveStepMode(config, platform, step) {
  if (!config || !step) return undefined;
  const id = typeof step === 'string' ? step : step.id;
  const children = queryMembers(config, id);
  if (children.length > 1) return same(children.map((member) => effectiveStepMode(config, platform, member)));
  const catalogStep = typeof step === 'string' ? config.task_step_catalog?.find((entry) => entry.id === id) : step;
  const fallback =
    config.home_only_pause_revision === HOME_ONLY_REVISION && !catalogStep?.home
      ? 'off'
      : config.frequency_groups?.some((group) => group.id === 'custom')
        ? 'custom'
        : config.frequency_groups?.[0]?.id;
  return config.task_modes?.[platform]?.[id] ?? config.platform_modes?.[platform]?.[id.split('@')[0]] ?? fallback;
}

export function stepParent(config, platform, stepId) {
  const children = queryMembers(config, stepId);
  if (children.length > 1) return same(children.map((member) => stepParent(config, platform, member)));
  return config?.task_layout?.assignments?.[platform]?.[stepId];
}

export function draftStepParent(config, platform, stepId, patch) {
  const children = queryMembers(config, stepId);
  if (children.length > 1) return same(children.map((member) => draftStepParent(config, platform, member, patch)));
  let target =
    patch.step_assignments?.find((item) => item.platform === platform && item.step_id === stepId)?.group_id ??
    stepParent(config, platform, stepId);
  const seen = new Set();
  while (patch.group_moves?.[target] && !seen.has(target)) {
    seen.add(target);
    target = patch.group_moves[target];
  }
  return target;
}

export function newTaskGroupId() {
  // randomUUID is unavailable on ordinary LAN HTTP pages. getRandomValues is
  // available there; the time/random fallback is only an identifier, not a key.
  const random = globalThis.crypto?.getRandomValues?.(new Uint32Array(4));
  return (
    'task_' +
    (random
      ? Array.from(random, (value) => value.toString(16).padStart(8, '0')).join('')
      : Date.now().toString(36) + Math.random().toString(36).slice(2))
  );
}

export function editorGroups(config, nodes = []) {
  const values = configs(config, nodes),
    groups = new Map();
  for (const current of values) {
    for (const group of current?.task_layout?.groups || []) {
      if (!groups.has(group.id)) groups.set(group.id, { id: group.id, name: group.name });
    }
  }
  return [...groups.values()].map((group) => ({
    ...group,
    missing: values.some((current) => !current?.task_layout?.groups?.some((entry) => entry.id === group.id)),
    mixed: values.some(
      (current) => current?.task_layout?.groups?.find((entry) => entry.id === group.id)?.name !== group.name,
    ),
  }));
}

export function editorSteps(config, nodes = [], platform) {
  const steps = new Map();
  for (const current of configs(config, nodes)) {
    for (const step of current?.task_step_catalog || []) {
      if (!platform || (step.platforms ? step.platforms.includes(platform) : hasBusinessAdapter(platform, step.kind))) {
        if (!steps.has(step.id)) steps.set(step.id, step);
      }
    }
  }
  return [...steps.values()];
}

function snapshots(data) {
  if (!data) return null;
  if (data.node_configs?.length) {
    if (data.node_configs.some((node) => node.online === false || node.error || !node.collection)) return null;
    return data.node_configs.map((node, index) => ({
      id: node.id ?? node.node_id ?? node.name ?? index,
      config: node.collection,
    }));
  }
  if (data.scope_node_id === 0 || !data.collection) return null;
  return [{ id: data.scope_node_id ?? 'local', config: data.collection }];
}

function resolvedMove(source, moves, deleted) {
  const seen = new Set([source]);
  let target = moves[source];
  while (deleted.has(target) && !seen.has(target)) {
    seen.add(target);
    target = moves[target];
  }
  return target && !deleted.has(target) ? target : undefined;
}

function matchesLayout(config, patch, before) {
  const layout = config.task_layout;
  if (!layout) return false;
  const groups = new Map((layout.groups || []).map((group) => [group.id, group]));
  const updates = patch.group_updates || {};
  const deleted = new Set(Object.keys(updates).filter((id) => updates[id] === null));
  for (const [id, update] of Object.entries(updates)) {
    if (update === null) {
      if (
        groups.has(id) ||
        Object.values(layout.assignments || {}).some((values) => Object.values(values).includes(id))
      )
        return false;
    } else if (!groups.has(id) || groups.get(id).name !== update.name?.trim()) return false;
  }
  const assignments = patch.step_assignments || [];
  if (assignments.some((item) => stepParent(config, item.platform, item.step_id) !== item.group_id)) return false;
  const moves = patch.group_moves || {};
  if (Object.keys(moves).length) {
    if (!before?.task_layout) return false;
    for (const [platform, steps] of Object.entries(before.task_layout.assignments || {})) {
      for (const [stepId, previousGroup] of Object.entries(steps)) {
        if (!deleted.has(previousGroup)) continue;
        const explicit = assignments.find((item) => item.platform === platform && item.step_id === stepId);
        const target = explicit?.group_id ?? resolvedMove(previousGroup, moves, deleted);
        if (!target || stepParent(config, platform, stepId) !== target) return false;
      }
    }
  }
  return true;
}

// Supply the pre-save scope when deleting a task so transfers are verified, not
// merely the disappearance of its label. Unknown/offline nodes never confirm.
export function confirmedLayoutPatch(patch, latest, before) {
  const actual = snapshots(latest);
  if (!actual?.length) return false;
  const original = before ? snapshots(before) : null;
  if (before && (!original || original.length !== actual.length)) return false;
  return actual.every((node) => {
    const previous = original?.find((entry) => entry.id === node.id);
    if (original && !previous) return false;
    return matchesLayout(node.config, patch, previous?.config);
  });
}

// Drop untouched fields before fan-out; differing node values are intentionally
// retained until the user explicitly chooses a shared value.
export function layoutPendingPatch(patch, baseline) {
  const values = baseline.node_configs?.length
    ? baseline.node_configs.map((node) => node.collection)
    : [baseline.collection];
  const all = (read) => values.length && values.every((config) => config && read(config));
  const group_updates = Object.fromEntries(
    Object.entries(patch.group_updates || {}).filter(
      ([id, update]) =>
        !all((config) => {
          const group = config.task_layout?.groups?.find((entry) => entry.id === id);
          return update === null
            ? !group &&
                !Object.values(config.task_layout?.assignments || {}).some((steps) => Object.values(steps).includes(id))
            : group?.name === update.name?.trim();
        }),
    ),
  );
  const step_assignments = (patch.step_assignments || []).filter(
    (item) => !all((config) => stepParent(config, item.platform, item.step_id) === item.group_id),
  );
  const group_moves = Object.fromEntries(
    Object.entries(patch.group_moves || {}).filter(([id]) => group_updates[id] === null),
  );
  return {
    ...(Object.keys(group_updates).length ? { group_updates } : {}),
    ...(step_assignments.length ? { step_assignments } : {}),
    ...(Object.keys(group_moves).length ? { group_moves } : {}),
  };
}

export function formatDuration(value) {
  if (value === null || value === undefined || value === '' || !Number.isFinite(Number(value)) || Number(value) < 0)
    return '暂无实测';
  const seconds = Number(value);
  if (seconds < 1) return `${Math.round(seconds * 10) / 10} 秒`;
  const rounded = Math.round(seconds);
  const hours = Math.floor(rounded / 3600),
    minutes = Math.floor((rounded % 3600) / 60),
    remainder = rounded % 60;
  return [
    hours && `${hours} 小时`,
    minutes && `${minutes} 分钟`,
    (!hours && !minutes) || remainder ? `${remainder} 秒` : '',
  ]
    .filter(Boolean)
    .join(' ');
}

export function formatCadence(group) {
  if (!group) return '未设置';
  return group.time ? `每日 ${group.time}` : `每 ${formatInterval(group.seconds)}`;
}
