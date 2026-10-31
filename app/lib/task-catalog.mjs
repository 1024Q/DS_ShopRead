export function taskLabel(task, names = {}) {
  return task.label || `${names[task.kind] || task.kind} · ${task.name}`;
}
export function taskAssignment(config, platform, task) {
  const explicit = config.task_modes?.[platform] || {};
  if (explicit[task.id] !== undefined) return explicit[task.id];
  const fallback = config.frequency_groups?.some((g) => g.id === 'custom')
    ? 'custom'
    : config.frequency_groups?.[0]?.id;
  const values = (task.members || [task.id]).map(
    (id) => explicit[id] ?? config.platform_modes?.[platform]?.[id.split('@')[0]] ?? fallback,
  );
  return values.every((v) => v === values[0]) ? values[0] : 'mixed';
}
