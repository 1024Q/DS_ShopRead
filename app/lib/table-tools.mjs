export const valueOf = (column, row) => (column.value ? column.value(row) : row[column.id]);
export function compareValues(a, b, type, direction = 'asc') {
  const empty = (v) => v === null || v === undefined || v === '';
  if (empty(a) || empty(b)) return empty(a) ? (empty(b) ? 0 : 1) : -1;
  const n = type === 'number' ? Number(a) - Number(b) : String(a).localeCompare(String(b), 'zh-CN', { numeric: false });
  return direction === 'desc' ? -n : n;
}
export function filterSort(rows, columns, keyword, filters, sort, direction) {
  const found = rows.filter(
    (r) =>
      (!keyword ||
        columns.some((c) =>
          String(valueOf(c, r) ?? '')
            .toLowerCase()
            .includes(keyword.toLowerCase()),
        )) &&
      columns.every(
        (c) =>
          !filters[c.id] ||
          String(valueOf(c, r) ?? '')
            .toLowerCase()
            .includes(filters[c.id].toLowerCase()),
      ),
  );
  const c = columns.find((c) => c.id === sort);
  return c ? [...found].sort((a, b) => compareValues(valueOf(c, a), valueOf(c, b), c.type, direction)) : found;
}
export function summarize(rows, columns) {
  return Object.fromEntries(
    columns
      .filter((c) => c.type === 'number')
      .map((c) => {
        let total = 0,
          missing = 0;
        for (const row of rows) {
          const v = valueOf(c, row);
          if (v === null || v === undefined || v === '' || !Number.isFinite(Number(v))) missing++;
          else total += Number(v);
        }
        return [c.id, { value: missing === rows.length ? null : total, missing }];
      }),
  );
}
