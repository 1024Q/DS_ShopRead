'use client';
import { Children, cloneElement, isValidElement, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { compareValues } from '../../lib/table-sort.mjs';
import { elements, rowValues, visibleRange } from '../../lib/table-values.mjs';
import { useOperatorColumns } from '../../providers/ShopOperators';
const items = (value) => Children.toArray(value);
export default function HomeSortableTable({ children, serverSort, resetKey, virtualize = true, ...props }) {
  const operatorColumns = useOperatorColumns(children, serverSort);
  children = operatorColumns.children;
  serverSort = operatorColumns.serverSort;
  const root = useRef(null),
    viewport = useRef(null),
    heights = useRef(new Map()),
    [sort, setSort] = useState({ index: -1, direction: 'asc' });
  const [scroll, setScroll] = useState(0),
    [revision, setRevision] = useState(0),
    [widths, setWidths] = useState([]);
  const model = useMemo(() => {
    const sections = items(children),
      body = sections.find((c) => c.type === 'tbody'),
      groups = items(body?.props.children);
    return { sections, body, groups };
  }, [children]);
  const { sections, body, groups } = model;
  const ordered = useMemo(
    () =>
      serverSort || sort.index < 0
        ? groups
        : groups
            .map((group) => ({ group, value: rowValues(group)[sort.index] }))
            .sort((a, b) => compareValues(a.value, b.value, sort.direction))
            .map((r) => r.group),
    [groups, !!serverSort, sort],
  );
  const nativeRows = useMemo(
    () =>
      ordered.map((group) => {
        const rows = elements(group);
        return rows.length === 1 && rows[0]?.type === 'tr' ? cloneElement(rows[0], { key: group.key }) : null;
      }),
    [ordered],
  );
  // Expanded/nested row groups keep their natural layout until collapsed.
  const virtual = virtualize && ordered.length >= 80 && nativeRows.every(Boolean);
  const anchor = useRef(null),
    previousReset = useRef(null);
  const orderKey = ordered.map((g) => g.key).join('|');
  const offsets = useMemo(() => {
    const values = [0];
    for (const group of ordered) values.push(values.at(-1) + (heights.current.get(group.key) || 88));
    return values;
  }, [ordered, revision]);
  const [start, end] = virtual ? visibleRange(offsets, scroll, 560) : [0, ordered.length];
  useLayoutEffect(() => {
    const reset = JSON.stringify([resetKey, sort.index, sort.direction, serverSort?.field, serverSort?.direction]);
    let top = 0;
    if (previousReset.current === reset && anchor.current) {
      const i = ordered.findIndex((r) => r.key === anchor.current.key);
      top = i < 0 ? Math.min(scroll, offsets.at(-1)) : offsets[i] + anchor.current.delta;
    }
    previousReset.current = reset;
    if (viewport.current) viewport.current.scrollTop = top;
    setScroll(top);
  }, [orderKey, resetKey, sort.index, sort.direction, serverSort?.field, serverSort?.direction]);
  useLayoutEffect(() => {
    const i = Math.max(
      0,
      offsets.findIndex((v, i) => i < ordered.length && offsets[i + 1] > scroll),
    );
    anchor.current = ordered[i] ? { key: ordered[i].key, delta: scroll - offsets[i] } : null;
  }, [scroll, offsets, ordered]);
  useLayoutEffect(() => {
    if (!virtual) {
      setWidths([]);
      return;
    }
    const table = root.current,
      host = viewport.current;
    if (!widths.length)
      setWidths(
        Array.from(table.tHead?.rows[0]?.cells || []).map((cell) => Math.ceil(cell.getBoundingClientRect().width)),
      );
    let frame = 0;
    const measure = () => {
      let changed = false;
      for (const row of table.tBodies[0]?.rows || []) {
        const key = row.dataset.virtualKey;
        if (!key) continue;
        const height = row.getBoundingClientRect().height;
        if (height && Math.abs((heights.current.get(key) || 0) - height) > 1) {
          heights.current.set(key, height);
          changed = true;
        }
      }
      if (changed) setRevision((v) => v + 1);
    };
    measure();
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(measure);
    });
    for (const row of table.tBodies[0]?.rows || []) if (row.dataset.virtualKey) observer.observe(row);
    observer.observe(host);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
    };
  }, [virtual, start, end, ordered, widths.length]);
  useLayoutEffect(() => {
    const keys = new Set(ordered.map((g) => g.key));
    for (const key of heights.current.keys()) if (!keys.has(key)) heights.current.delete(key);
  }, [orderKey]);
  function header(th, index) {
    if (!isValidElement(th) || th.type !== 'th') return th;
    const label = typeof th.props.children === 'string' ? th.props.children : '';
    const field = serverSort?.fields?.[index],
      enabled = serverSort
        ? !!field
        : !['操作', '主图', '选择', '恢复预览', '临时优先'].includes(label) && !(th.props.colSpan > 1);
    if (!enabled) return th;
    const selected = serverSort ? serverSort.field === field : sort.index === index,
      direction = selected ? serverSort?.direction || sort.direction : '';
    return cloneElement(
      th,
      { 'aria-sort': selected ? (direction === 'asc' ? 'ascending' : 'descending') : 'none' },
      <button
        type="button"
        className="column-sort"
        data-direction={direction}
        title={`${label}${serverSort ? '' : '（当前已加载记录）'}：点击${direction === 'asc' ? '降序' : '升序'}排序`}
        onClick={() => {
          const next = direction === 'asc' ? 'desc' : 'asc';
          serverSort ? serverSort.onChange(field, next) : setSort({ index, direction: next });
        }}
      >
        {th.props.children}
        <i aria-hidden="true" />
      </button>,
    );
  }
  const columnCount = items(sections.find((s) => s.type === 'thead')?.props.children)[0]?.props.children;
  const spacer = (key, height) => (
    <tr key={key} className="virtual-spacer" aria-hidden="true">
      <td colSpan={items(columnCount).length || 1} style={{ height, padding: 0, border: 0 }} />
    </tr>
  );
  const rendered = virtual
    ? [
        spacer('virtual-top', offsets[start]),
        ...nativeRows
          .slice(start, end)
          .map((row, i) =>
            cloneElement(row, { 'data-virtual-key': ordered[start + i].key, 'aria-rowindex': start + i + 2 }),
          ),
        spacer('virtual-bottom', offsets.at(-1) - offsets[end]),
      ]
    : ordered;
  const table = (
    <table
      {...props}
      ref={root}
      data-virtualized={virtual || undefined}
      aria-rowcount={virtual ? ordered.length + 1 : undefined}
      style={{
        ...props.style,
        ...(virtual && widths.length
          ? { tableLayout: 'fixed', width: widths.reduce((a, b) => a + b, 0), minWidth: '100%' }
          : {}),
      }}
    >
      {virtual && widths.length > 0 && (
        <colgroup>
          {widths.map((width, i) => (
            <col key={i} data-base-width={width} style={{ width }} />
          ))}
        </colgroup>
      )}
      {sections.map((section) =>
        section.type === 'thead'
          ? cloneElement(
              section,
              {},
              items(section.props.children).map((tr) => cloneElement(tr, {}, items(tr.props.children).map(header))),
            )
          : section === body
            ? cloneElement(section, {}, rendered)
            : section,
      )}
    </table>
  );
  return virtual ? (
    <div
      className="virtual-table-viewport"
      ref={viewport}
      tabIndex={0}
      role="region"
      aria-label={`数据表格，共 ${ordered.length} 行，可滚动查看更多`}
      onScroll={(e) => setScroll(e.currentTarget.scrollTop)}
    >
      {table}
    </div>
  ) : (
    table
  );
}
