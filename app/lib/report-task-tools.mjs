export function filterReportJobs(jobs, { shop = '', month = '', state = '' } = {}) {
  return jobs.filter(
    (j) =>
      (!shop || String(j.shop_id) === String(shop)) &&
      (!month || j.start?.slice(0, 7) === month) &&
      (!state || j.state === state),
  );
}
export function reportStorage(jobs) {
  return jobs.reduce(
    (sum, j) => ({
      center: sum.center + (Number(j.center_file_bytes) || 0),
      node: sum.node + (Number(j.node_file_bytes) || 0),
      unknown: sum.unknown + (Number.isFinite(j.node_file_bytes) ? 0 : 1),
      rows: sum.rows + (Number(j.row_count) || 0),
    }),
    { center: 0, node: 0, unknown: 0, rows: 0 },
  );
}
export function formatBytes(value) {
  if (!Number.isFinite(value) || value < 0) return '未回报';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}
