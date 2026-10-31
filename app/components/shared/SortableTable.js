'use client';
import { Children, createContext, useContext, useMemo, useState } from 'react';
import { Table } from 'antd';
import { compareValues } from '../../lib/table-sort.mjs';
import { columnWidth } from '../../lib/column-width.mjs';
import { columnClass } from '../../lib/table-pinning.mjs';
import { elements, rowValues } from '../../lib/table-values.mjs';
import { useOperatorColumns } from '../../providers/ShopOperators';
import { useAdminWorkspace } from '../../providers/AdminWorkspace';
import HomeSortableTable from '../overview/HomeSortableTable';
const list = Children.toArray;
const RowGroups = createContext(new Map());
function GroupRow(props) {
  return useContext(RowGroups).get(String(props['data-row-key'])) || null;
}
// Component row groups (including live source rows) retain their lifecycle and expansion.
export default function SortableTable(props) {
  const admin = useAdminWorkspace();
  return admin ? <AdminTable {...props} /> : <HomeSortableTable {...props} />;
}
function AdminTable({ children, serverSort, resetKey, virtualize = true, className = '', ...props }) {
  const transformed = useOperatorColumns(children, serverSort);
  children = transformed.children;
  serverSort = transformed.serverSort;
  const [sort, setSort] = useState({ index: -1, direction: 'asc' });
  const sections = list(children),
    head = list(sections.find((s) => s.type === 'thead')?.props.children)[0],
    headers = list(head?.props.children),
    groups = list(sections.find((s) => s.type === 'tbody')?.props.children);
  const records = groups.map((g, i) => {
    const row = elements(g)[0];
    return { key: g.key ?? i, group: g, row, cells: elements(row?.props?.children), values: rowValues(g) };
  });
  const grouped = groups.some((g) => {
    const rows = elements(g);
    return (
      rows.length !== 1 ||
      rows[0]?.type !== 'tr' ||
      elements(rows[0].props.children).some((cell) => !['td', 'th'].includes(cell?.type))
    );
  });
  const groupMap = new Map(records.map((r) => [String(r.key), r.group]));
  const ordered =
    serverSort || sort.index < 0
      ? records
      : [...records].sort((a, b) => compareValues(a.values[sort.index], b.values[sort.index], sort.direction));
  const columns = headers.map((h, index) => {
    const label = typeof h.props.children === 'string' ? h.props.children : '',
      field = serverSort?.fields?.[index];
    const enabled = serverSort
      ? !!field
      : !['操作', '主图', '选择', '恢复预览', '临时优先'].includes(label) && !(h.props.colSpan > 1);
    const selected = serverSort ? serverSort.field === field : sort.index === index,
      direction = serverSort?.direction || sort.direction;
    return {
      key: String(index),
      title: h.props.children,
      width: columnWidth(label, index),
      sorter: enabled,
      sortDirections: ['ascend', 'descend'],
      sortOrder: selected ? (direction === 'asc' ? 'ascend' : 'descend') : null,
      onHeaderCell: () => ({ className: columnClass(label, index, headers.length) }),
      render: (_, record) => record.cells[index]?.props.children,
      onCell: (record) => {
        const cell = record.cells[index];
        if (!cell) return { colSpan: 0 };
        const { children, ...p } = cell.props;
        return { ...p, className: [p.className, columnClass(label, index, headers.length)].filter(Boolean).join(' ') };
      },
    };
  });
  const NativeTable = useMemo(
    () =>
      function NativeTable(p) {
        return <table {...p} className={`ant-business-grid ${p.className || ''} ${className}`} />;
      },
    [className],
  );
  return (
    <RowGroups.Provider value={groupMap}>
      <Table
        {...props}
        className="admin-business-table"
        size="middle"
        tableLayout="fixed"
        columns={columns}
        dataSource={ordered}
        pagination={false}
        components={{ table: NativeTable, ...(grouped ? { body: { row: GroupRow } } : {}) }}
        scroll={{
          x: Math.max(
            800,
            columns.reduce((n, c) => n + c.width, 0),
          ),
          ...(virtualize && records.length >= 80 ? { y: 560 } : {}),
        }}
        rowClassName={(r) => r.row?.props?.className || ''}
        onRow={(r) => {
          const { children, className, ...rest } = r.row?.props || {};
          return rest;
        }}
        onChange={(_, filters, next) => {
          const index = Number(next.columnKey),
            direction = next.order === 'descend' ? 'desc' : 'asc';
          if (serverSort) serverSort.onChange(serverSort.fields[index], direction);
          else setSort({ index, direction });
        }}
      />
    </RowGroups.Provider>
  );
}
