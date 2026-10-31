export function formatInterval(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) return '—';
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`;
  if (seconds % 60 === 0) return `${seconds / 60} 分钟`;
  return `${seconds} 秒`;
}

export function groupSourceRows(rows, groups = []) {
  const result = new Map();
  for (const row of rows) {
    const id = row.queue || 'unknown';
    if (!result.has(id))
      result.set(id, {
        id,
        name: row.queue_names?.[id] || groups.find((g) => g.id === id)?.name || (id === 'off' ? '暂停采集' : id),
        rows: [],
      });
    result.get(id).rows.push(row);
  }
  const order = new Map(groups.map((g, i) => [g.id, i]));
  return [...result.values()].sort((a, b) => (order.get(a.id) ?? 999) - (order.get(b.id) ?? 999));
}
