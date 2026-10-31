'use client';
import { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Collapse,
  Drawer,
  Dropdown,
  Empty,
  Input,
  InputNumber,
  Pagination,
  Space,
  Table,
  Tag,
  Tooltip,
} from 'antd';
import {
  DownloadOutlined,
  SettingOutlined,
  ReloadOutlined,
  HistoryOutlined,
  SearchOutlined,
  FilterOutlined,
  ArrowUpOutlined,
  ArrowDownOutlined,
  CopyOutlined,
} from '@ant-design/icons';
import { filterSort, summarize, valueOf } from '../../lib/table-tools.mjs';
const emptyRows = [],
  emptyColumns = [];
const number = (v) => (v == null ? '—' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 6 }));
async function api(path, body, signal) {
  const r = await fetch('/api/' + path, {
    method: body ? 'POST' : 'GET',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(60000)]) : AbortSignal.timeout(60000),
  });
  const d = await r.json();
  if (!r.ok) throw Error(d.detail || '操作失败');
  return d;
}
export default function DataTable({
  id,
  title,
  rows: inputRows = emptyRows,
  columns: inputColumns = emptyColumns,
  source,
  query = {},
  costId,
  refreshKey = 0,
  selectedIds,
  onSelectionChange,
  disabled = false,
}) {
  const [keyword, setKeyword] = useState(''),
    [filters, setFilters] = useState({}),
    [sort, setSort] = useState(''),
    [direction, setDirection] = useState('asc'),
    [page, setPage] = useState(1),
    [size, setSize] = useState(50),
    [selection, setSelection] = useState([]),
    [all, setAll] = useState(false),
    [prefs, setPrefs] = useState({}),
    [ready, setReady] = useState(false),
    [result, setResult] = useState(null),
    [loading, setLoading] = useState(false),
    [error, setError] = useState(''),
    [jobs, setJobs] = useState([]),
    [tick, setTick] = useState(0),
    [exporting, setExporting] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false),
    [historyOpen, setHistoryOpen] = useState(false);
  const queryKey = JSON.stringify(query),
    filterKey = JSON.stringify(filters),
    server = source === 'orders';
  const requestKey = JSON.stringify([
    server,
    costId,
    queryKey,
    server ? keyword : '',
    server ? filterKey : '',
    server ? sort : '',
    server ? direction : '',
    server ? page : 0,
    server ? size : 0,
    tick,
    refreshKey,
  ]);
  const remote = result?.key === requestKey ? result.data : null,
    dataCurrent = !(server || costId) || !!remote;
  const pendingExports = jobs.some((j) => !['completed', 'error'].includes(j.state));
  useEffect(() => {
    try {
      const p = JSON.parse(localStorage.getItem('table:' + id) || '{}');
      setPrefs(p);
    } catch {}
    setReady(true);
  }, [id]);
  useEffect(() => {
    if (ready) localStorage.setItem('table:' + id, JSON.stringify(prefs));
  }, [prefs, ready, id]);
  useEffect(() => {
    setSelection([]);
    setAll(false);
    setPage(1);
  }, [queryKey, costId]);
  useEffect(() => {
    if (!server && !costId) return;
    let alive = true;
    const controller = new AbortController();
    setLoading(true);
    setError('');
    const t = setTimeout(() => {
      const promise = server
        ? api(
            'table-tools/orders',
            { ...query, keyword: keyword || query.keyword || '', filters, sort, direction, page, page_size: size },
            controller.signal,
          )
        : api('table-tools/cost/' + costId, undefined, controller.signal);
      promise
        .then((d) => {
          if (alive) {
            setResult({ key: requestKey, data: d });
            setError('');
          }
        })
        .catch((e) => {
          if (alive) {
            setResult(null);
            setError(e.message);
          }
        })
        .finally(() => {
          if (alive) setLoading(false);
        });
    }, 200);
    return () => {
      alive = false;
      clearTimeout(t);
      controller.abort();
    };
  }, [requestKey]);
  useEffect(() => {
    if (!historyOpen && !pendingExports) return;
    let alive = true,
      timer,
      busy = false;
    const controller = new AbortController();
    async function poll() {
      clearTimeout(timer);
      if (document.hidden || busy || !alive) return;
      busy = true;
      try {
        const d = await api('table-tools/exports', undefined, controller.signal);
        if (alive)
          setJobs((old) => {
            const next = d.filter((j) => j.title === title);
            return JSON.stringify(old) === JSON.stringify(next) ? old : next;
          });
      } catch {
      } finally {
        busy = false;
        if (alive && !document.hidden) timer = setTimeout(poll, 5000);
      }
    }
    const visible = () => {
      if (document.hidden) clearTimeout(timer);
      else poll();
    };
    poll();
    document.addEventListener('visibilitychange', visible);
    return () => {
      alive = false;
      clearTimeout(timer);
      controller.abort();
      document.removeEventListener('visibilitychange', visible);
    };
  }, [title, historyOpen, pendingExports]);
  const columns = useMemo(
    () =>
      server
        ? remote?.columns || emptyColumns
        : costId
          ? (remote?.headers || emptyColumns).map((h, i) => ({
              id: 'raw_' + i,
              label: h,
              type: [remote?.mapping?.cost_column, remote?.mapping?.shipping_column].includes(i) ? 'number' : 'text',
            }))
          : inputColumns,
    [server, costId, remote, inputColumns],
  );
  const rows = server || costId ? remote?.rows || emptyRows : inputRows;
  const filtered = useMemo(
    () => (server ? rows : filterSort(rows, columns, keyword, filters, sort, direction)),
    [rows, columns, keyword, filterKey, sort, direction, server],
  );
  const total = server ? remote?.total || 0 : filtered.length,
    pages = Math.max(1, Math.ceil(total / size)),
    currentPage = Math.min(page, pages),
    display = server ? rows : filtered.slice((currentPage - 1) * size, currentPage * size);
  const ids = selectedIds ?? selection,
    setIds = onSelectionChange ?? setSelection,
    selected = new Set(ids),
    selectedCount = server
      ? all
        ? Math.max(0, total - ids.length)
        : ids.length
      : filtered.filter((r) => selected.has(String(r.id))).length;
  const summary = useMemo(
    () => (server ? remote?.summary || {} : summarize(filtered, columns)),
    [server, remote, filtered, columns],
  );
  const order = [
    ...(prefs.order || []).filter((k) => columns.some((c) => c.id === k)),
    ...columns.map((c) => c.id).filter((k) => !(prefs.order || []).includes(k)),
  ];
  const shown = order
    .map((k) => columns.find((c) => c.id === k))
    .filter((c) => !(prefs.hidden || ['book']).includes(c.id));
  const checked = (r) => (all ? !selected.has(String(r.id)) : selected.has(String(r.id)));
  function toggle(r) {
    const key = String(r.id);
    setIds(ids.includes(key) ? ids.filter((x) => x !== key) : [...ids, key]);
  }
  function changeFilter(fn, v) {
    fn(v);
    setPage(1);
    setIds([]);
    setAll(false);
  }
  function shift(key, delta) {
    const next = [...order],
      i = next.indexOf(key),
      j = i + delta;
    if (j < 0 || j >= next.length) return;
    [next[i], next[j]] = [next[j], next[i]];
    setPrefs((p) => ({ ...p, order: next }));
  }
  async function exportFile(format, onlySelected) {
    if (!dataCurrent || loading || disabled) return;
    setExporting(true);
    setError('');
    try {
      const cols = order.map((k) => columns.find((c) => c.id === k)).filter((c) => c.type !== 'action');
      const body = {
        format,
        title,
        query: {
          ...query,
          ...(costId ? { cost_book_id: costId, cost_book_name: remote?.name } : {}),
          keyword: keyword || query.keyword || '',
          filters,
          sort,
          direction,
        },
        column_ids: cols.map((c) => c.id),
      };
      if (server) {
        body.source = 'orders';
        if (onlySelected) body.selection = { all, ids };
      } else {
        body.columns = cols.map((c) => ({ id: c.id, label: c.label, type: c.type }));
        body.rows = filtered
          .filter((r) => !onlySelected || checked(r))
          .map((r) => Object.fromEntries(cols.map((c) => [c.id, valueOf(c, r) ?? null])));
      }
      await api('table-tools/exports', body);
      setJobs((await api('table-tools/exports')).filter((j) => j.title === title));
      setHistoryOpen(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setExporting(false);
    }
  }
  const tableColumns = shown.map((c, index) => ({
    key: c.id,
    title: c.label,
    width: prefs.widths?.[c.id] || (c.type === 'action' ? 200 : index === 0 ? 240 : 180),
    fixed: index === 0 ? 'left' : c.type === 'action' ? 'right' : undefined,
    align: c.type === 'number' ? 'right' : 'left',
    sorter: c.type !== 'action' && !disabled,
    sortOrder: sort === c.id ? (direction === 'asc' ? 'ascend' : 'descend') : null,
    filteredValue: filters[c.id] ? [filters[c.id]] : null,
    filterIcon:
      c.type !== 'action'
        ? (filtered) => <FilterOutlined style={{ color: filtered ? '#3478ed' : undefined }} />
        : undefined,
    filterDropdown:
      c.type !== 'action'
        ? ({ close }) => (
            <div className="admin-column-filter">
              <Input
                autoFocus
                aria-label={'筛选' + c.label}
                placeholder={'筛选' + c.label}
                value={filters[c.id] || ''}
                disabled={disabled}
                onChange={(e) => changeFilter(setFilters, { ...filters, [c.id]: e.target.value })}
                onPressEnter={close}
              />
              <Button
                size="small"
                onClick={() => {
                  changeFilter(setFilters, { ...filters, [c.id]: '' });
                  close();
                }}
              >
                清除
              </Button>
              <Button size="small" type="primary" onClick={close}>
                完成
              </Button>
            </div>
          )
        : undefined,
    render: (_, r) => {
      const v = valueOf(c, r);
      return c.render ? (
        c.render(r)
      ) : (
        <span className={c.type === 'number' ? 'admin-number' : 'admin-cell'} title={v == null ? '' : String(v)}>
          {c.type === 'number' ? number(v) : (v ?? '—')}
          {c.copy && v && (
            <Tooltip title="复制">
              <Button
                type="text"
                size="small"
                icon={<CopyOutlined />}
                onClick={() => navigator.clipboard.writeText(String(v)).catch(() => setError('复制失败，请手动复制'))}
              />
            </Tooltip>
          )}
        </span>
      );
    },
  }));
  const exportItems = [
    { key: 'xlsx-all', label: '全部筛选结果 · Excel' },
    { key: 'csv-all', label: '全部筛选结果 · CSV' },
    { type: 'divider' },
    { key: 'xlsx-selected', label: '选中记录 · Excel', disabled: !selectedCount },
    { key: 'csv-selected', label: '选中记录 · CSV', disabled: !selectedCount },
  ];
  return (
    <section className="admin-table" aria-label={title}>
      <div className="admin-table-toolbar">
        <div className="admin-table-title">
          <strong>{title}</strong>
          <Tag>{total.toLocaleString()} 条</Tag>
        </div>
        <Space wrap>
          <Input
            allowClear
            prefix={<SearchOutlined />}
            aria-label={title + '搜索'}
            placeholder="搜索筛选结果"
            value={keyword}
            disabled={disabled}
            onChange={(e) => changeFilter(setKeyword, e.target.value)}
            style={{ width: 220 }}
          />
          <Button
            disabled={disabled || loading}
            onClick={() => {
              setKeyword('');
              setFilters({});
              setSort('');
              setPage(1);
              setIds([]);
              setAll(false);
            }}
          >
            重置
          </Button>
          {(server || costId) && (
            <Tooltip title="刷新数据">
              <Button
                aria-label="刷新数据"
                disabled={disabled || loading}
                icon={<ReloadOutlined />}
                onClick={() => setTick((v) => v + 1)}
              />
            </Tooltip>
          )}
          <Dropdown
            trigger={['click']}
            menu={{ items: exportItems, onClick: ({ key }) => exportFile(key.split('-')[0], key.endsWith('selected')) }}
          >
            <Button
              type="primary"
              icon={<DownloadOutlined />}
              loading={exporting}
              disabled={disabled || loading || !dataCurrent || !total}
            >
              导出
            </Button>
          </Dropdown>
          <Tooltip title="列设置">
            <Button aria-label="列设置" icon={<SettingOutlined />} onClick={() => setSettingsOpen(true)} />
          </Tooltip>
          <Tooltip title="导出记录">
            <Button aria-label="导出记录" icon={<HistoryOutlined />} onClick={() => setHistoryOpen(true)} />
          </Tooltip>
        </Space>
      </div>
      {!!selectedCount && (
        <div className="admin-selection">
          <span>
            已选 <strong>{selectedCount.toLocaleString()}</strong> 条
          </span>
          <Button
            type="link"
            disabled={disabled || loading || !dataCurrent}
            onClick={() => {
              if (server) {
                setAll(true);
                setIds([]);
              } else setIds(filtered.map((r) => String(r.id)));
            }}
          >
            选择全部筛选结果（{total.toLocaleString()}）
          </Button>
          <Button
            type="link"
            disabled={disabled}
            onClick={() => {
              setAll(false);
              setIds([]);
            }}
          >
            取消选择
          </Button>
        </div>
      )}
      {error && (
        <Alert
          type="error"
          showIcon
          title={error}
          description={!dataCurrent ? '当前筛选结果未读取成功，已清除旧结果；请点击刷新数据重试。' : undefined}
        />
      )}
      <Table
        rowKey={(r) => String(r.id)}
        size="middle"
        columns={tableColumns}
        dataSource={display}
        loading={loading}
        pagination={false}
        scroll={{ x: 'max-content', y: 520 }}
        locale={{ emptyText: <Empty description="没有符合条件的数据" /> }}
        onChange={(_, __, s) => {
          setSort(s.order ? s.columnKey : '');
          setDirection(s.order === 'descend' ? 'desc' : 'asc');
          setPage(1);
        }}
        rowSelection={{
          fixed: true,
          columnWidth: 44,
          selectedRowKeys: display.filter(checked).map((r) => String(r.id)),
          getCheckboxProps: () => ({ disabled: disabled || loading || !dataCurrent }),
          onSelect: (r) => toggle(r),
          onSelectAll: (isSelected, _, changed) => {
            const next = new Set(ids);
            for (const r of changed) {
              const key = String(r.id);
              if (isSelected !== all) next.add(key);
              else next.delete(key);
            }
            setIds([...next]);
          },
        }}
      />
      <div className="admin-table-footer">
        <span>汇总和导出均覆盖全部筛选结果</span>
        <Pagination
          current={currentPage}
          pageSize={size}
          total={total}
          showSizeChanger
          pageSizeOptions={[20, 50, 100, 200]}
          disabled={loading}
          onChange={(p, n) => {
            setPage(n !== size ? 1 : p);
            setSize(n);
          }}
        />
      </div>
      <Collapse
        ghost
        items={[
          {
            key: 'totals',
            label: '金额与数量汇总',
            children: (
              <div className="admin-totals">
                {columns
                  .filter((c) => c.type === 'number')
                  .map((c) => (
                    <div key={c.id}>
                      <span>{c.label}</span>
                      <strong>{number(summary[c.id]?.value)}</strong>
                      {summary[c.id]?.missing > 0 && <small>已知部分 · 缺 {summary[c.id].missing} 行</small>}
                    </div>
                  ))}
              </div>
            ),
          },
        ]}
      />
      <Drawer
        title="列设置"
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        size={460}
        extra={<Button onClick={() => setPrefs({})}>恢复默认</Button>}
      >
        <p>调整显示列、顺序和宽度，自动保存在此浏览器。</p>
        {order.map((k, i) => {
          const c = columns.find((c) => c.id === k);
          return (
            <div className="admin-column-setting" key={k}>
              <Checkbox
                checked={!(prefs.hidden || ['book']).includes(k)}
                onChange={(e) =>
                  setPrefs((p) => ({
                    ...p,
                    hidden: e.target.checked
                      ? (p.hidden || ['book']).filter((x) => x !== k)
                      : [...(p.hidden || ['book']), k],
                  }))
                }
              >
                {c.label}
              </Checkbox>
              <Space size={4}>
                <Button
                  aria-label={c.label + '前移'}
                  size="small"
                  icon={<ArrowUpOutlined />}
                  disabled={!i}
                  onClick={() => shift(k, -1)}
                />
                <Button
                  aria-label={c.label + '后移'}
                  size="small"
                  icon={<ArrowDownOutlined />}
                  disabled={i === order.length - 1}
                  onClick={() => shift(k, 1)}
                />
                <InputNumber
                  aria-label={c.label + '列宽'}
                  min={100}
                  max={800}
                  value={prefs.widths?.[k] || 180}
                  onChange={(v) => setPrefs((p) => ({ ...p, widths: { ...p.widths, [k]: v || 180 } }))}
                />
              </Space>
            </div>
          );
        })}
      </Drawer>
      <Drawer title="后台导出记录" open={historyOpen} onClose={() => setHistoryOpen(false)} size={480}>
        <Alert
          type="info"
          showIcon
          title="导出文件保留7天"
          description="导出包含全部筛选结果和数据列，不限当前页。编码推荐使用Excel格式保留前导零、长数字；此文件不是补充成本上传模板。"
        />
        {!jobs.length && <Empty description="暂无导出记录" />}
        {jobs.map((j) => (
          <div className="admin-export-job" key={j.id}>
            <div>
              <strong>{j.format.toUpperCase()}</strong> · {new Date(j.created * 1000).toLocaleString('zh-CN')}
            </div>
            <Tag color={j.state === 'completed' ? 'success' : j.state === 'error' ? 'error' : 'processing'}>
              {j.state === 'completed' ? '已完成' : j.state === 'error' ? '失败' : `正在处理 ${j.written || 0} 行`}
            </Tag>
            {j.error && <p>{j.error}</p>}
            {j.state === 'completed' && (
              <Button type="link" icon={<DownloadOutlined />} href={'/api/table-tools/exports/' + j.id + '/download'}>
                下载文件
              </Button>
            )}
          </div>
        ))}
      </Drawer>
    </section>
  );
}
