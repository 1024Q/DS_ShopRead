export const scheduleFields = [
  'enabled',
  'realtime_seconds',
  'daily_time',
  'custom_seconds',
  'custom2_seconds',
  'custom3_seconds',
  'frequency_names',
  'keepalive_enabled',
  'keepalive_seconds',
  'stagger_seconds',
  'retry_concurrent',
  'batch_pages',
  'batch_seconds',
  'adaptive_enabled',
  'change_adaptive_enabled',
];
export function settingsPatch(before, after) {
  const patch = {};
  if (!before || !after) return patch;
  const old = Object.fromEntries((before.frequency_groups || []).map((g) => [g.id, g])),
    next = Object.fromEntries((after.frequency_groups || []).map((g) => [g.id, g]));
  const updates = Object.fromEntries(
    [...new Set([...Object.keys(old), ...Object.keys(next)])]
      .filter((id) => JSON.stringify(old[id]) !== JSON.stringify(next[id]))
      .map((id) => [
        id,
        !next[id]
          ? null
          : !old[id]
            ? next[id]
            : Object.fromEntries(Object.entries(next[id]).filter(([k, v]) => old[id][k] !== v)),
      ]),
  );
  if (Object.keys(updates).length) {
    patch.group_updates = updates;
    patch.group_moves = after.group_moves || {};
  }

  for (const key of scheduleFields) {
    if (key === 'frequency_names') {
      const names = Object.fromEntries(
        Object.entries(after[key] || {}).filter(([id, name]) => name !== before[key]?.[id]),
      );
      if (Object.keys(names).length) patch[key] = names;
    } else if (after[key] !== before[key]) patch[key] = after[key];
  }
  return patch;
}
