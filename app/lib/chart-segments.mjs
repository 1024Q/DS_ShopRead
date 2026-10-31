// Preserve missing observations instead of implying measured continuity.
export function chartSegments(rows, metric, point) {
  const result = [];
  let segment = [],
    previous = null,
    previousCohort = null;
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i],
      value = row[metric];
    const stamp = row.date
      ? Date.parse(row.date + 'T00:00:00Z') + (Number(row.hour) || 0) * 3600000
      : row.hour != null
        ? Number(row.hour) * 3600000
        : null;
    const step = row.hour == null ? 86400000 : 3600000;
    if (
      (previousCohort != null && row.cohort != null && previousCohort !== row.cohort) ||
      value == null ||
      !Number.isFinite(Number(value)) ||
      (previous != null && stamp != null && stamp - previous > step * 1.5)
    ) {
      if (segment.length) result.push(segment.join(' '));
      segment = [];
    }
    if (value != null && Number.isFinite(Number(value))) segment.push(point(row, i));
    previous = stamp;
    previousCohort = row.cohort;
  }
  if (segment.length) result.push(segment.join(' '));
  return result;
}
