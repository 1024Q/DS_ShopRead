'use client';
import { NativeButton, TextInput, Choice } from '../shared/AdminControls';
import useDebounced from '../../hooks/useDebounced';
import { sortedRows } from '../../lib/table-sort.mjs';
import MultiSelect from '../shared/MultiSelect';
import usePersistentSelection from '../../hooks/usePersistentSelection';
import { useEffect, useRef, useState } from 'react';
import { useWorkspace, useGlobalRefresh } from '../../providers/WorkspaceContext';
import { nodeApi } from '../settings/RemoteMachines';
import { platforms } from '../../lib/platforms.mjs';
import { shopLabel } from '../../lib/shop-identity.mjs';
import CollapsibleSection from '../shared/CollapsibleSection';
import SortableTable from '../shared/SortableTable';
export default function FleetDashboard() {
  const [data, setData] = useState(null),
    [platform, setPlatform] = usePersistentSelection('fleet-platform'),
    [operator, setOperator] = useState(''),
    [machine, setMachine] = useState(''),
    [error, setError] = useState(''),
    [version, setVersion] = useState(0);
  const [page, setPage] = useState(1),
    [sort, setSort] = useState({});
  const settledOperator = useDebounced(operator);
  useEffect(() => setPage(1), [platform, settledOperator, machine]);
  const { auto } = useWorkspace(),
    automatic = useRef(auto);
  automatic.current = auto;
  useGlobalRefresh(() => setVersion((v) => v + 1));
  useEffect(() => {
    let alive = true,
      busy = false;
    const c = new AbortController();
    async function load() {
      if (busy || document.hidden) return;
      busy = true;
      try {
        const q = new URLSearchParams({ platform, operator: settledOperator });
        const r = await fetch('/api/fleet?' + q, { signal: c.signal, cache: 'no-store' });
        const d = await r.json();
        if (!r.ok) throw Error(d.detail || '汇总读取失败');
        if (alive) {
          setData(d);
          setError('');
        }
      } catch (e) {
        if (alive && e.name !== 'AbortError') setError(e.message);
      } finally {
        busy = false;
      }
    }
    load();
    const timer = setInterval(() => {
      if (automatic.current) load();
    }, 60000);
    return () => {
      alive = false;
      c.abort();
      clearInterval(timer);
    };
  }, [platform, settledOperator, version]);
  async function open(row, view) {
    try {
      await nodeApi('active-node', { id: row.node_id });
      sessionStorage.setItem(
        'commerce:navigate',
        JSON.stringify({ view, filters: { shop_id: String(row.shop_id) }, platform: row.platform }),
      );
      window.location.reload();
    } catch (e) {
      setError(e.message);
    }
  }
  const rows = (data?.rows || []).filter((r) => !machine || String(r.node_id) === machine),
    money = (v) => (v == null ? '—' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 }));
  return (
    <section className="business-panel fleet-dashboard">
      <div className="business-heading">
        <div>
          <h2>跨机器汇总</h2>
          <p>每 60 秒更新</p>
        </div>
      </div>
      <div className="filter-panel">
        <label>
          平台
          <MultiSelect
            label="平台筛选"
            value={platform}
            onChange={(value) => setPlatform(value)}
            options={Object.entries(platforms).map(([value, label]) => ({ value, label }))}
            placeholder="全部平台"
          />
        </label>
        <label>
          运营负责人
          <TextInput
            list="fleet-operators"
            value={operator}
            onChange={(e) => setOperator(e.target.value)}
            placeholder="搜索负责人"
          />
          <datalist id="fleet-operators">
            {[...new Set((data?.rows || []).flatMap((r) => r.operators || []))].map((n) => (
              <option key={n} value={n} />
            ))}
          </datalist>
        </label>
      </div>
      {error && (
        <p role="status" className="error">
          {error} · 保留上次结果
        </p>
      )}
      <h3>全部机器 · 当前平台与运营范围合计</h3>
      <div className="fleet-kpis">
        {[
          ['sales', '今日成交额（元）'],
          ['orders', '今日成交订单'],
          ['ad_spend', '推广花费（元）'],
          ['pending_aftersales', '待处理售后'],
        ].map(([key, title]) => (
          <article key={key}>
            <span>{title}</span>
            <strong>{money(data?.totals[key])}</strong>
            <small>
              {data?.coverage[key] ?? 0} / {data?.unique_shops ?? 0} 个去重账号有可汇总数据
            </small>
          </article>
        ))}
      </div>
      <details className="context-note">
        <summary>统计说明</summary>
        <p>
          上方汇总按平台、负责人筛选，包含所有机器；离线、跨日未更新数据不计入当前总额。同一账号只取一份，同名不同账号分别统计；跨平台成交口径沿用各平台原始口径。
        </p>
      </details>
      <CollapsibleSection className="settings-card">
        <div className="section-head">
          <h3>机器连接状态</h3>
        </div>
        <div className="health-grid">
          {data?.machines.map((m) => (
            <div className="health-shop" key={m.node_id}>
              <strong>{m.name}</strong>
              <span>{m.duplicate ? '重复机器，已排除' : m.online ? '摘要已连接' : '离线 / 待更新'}</span>
              <small>
                {m.error || '已完成摘要汇入'} ·{' '}
                {m.updated_at
                  ? new Date(m.updated_at * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
                  : '尚无快照'}
              </small>
            </div>
          ))}
        </div>
      </CollapsibleSection>
      <div className="table-caption">
        <strong>店铺明细 · 仅筛选下方表格</strong>
        <label>
          明细机器
          <Choice value={machine} onChange={(e) => setMachine(e.target.value)}>
            <option value="">全部机器</option>
            {data?.machines.map((m) => (
              <option key={m.node_id} value={m.node_id}>
                {m.name}
              </option>
            ))}
          </Choice>
        </label>
      </div>
      <div className="data-table-wrap">
        <SortableTable
          className="data-table"
          serverSort={{
            fields: ['name', 'state', 'sales', 'orders', 'attention', 'operator', 'updated_at'],
            ...sort,
            onChange: (field, direction) => {
              setSort({ field, direction });
              setPage(1);
            },
          }}
        >
          <thead>
            <tr>
              {['机器 / 店铺', '状态', '成交额', '订单', '重点待办', '负责人', '采集时间', '操作'].map((s) => (
                <th key={s}>{s}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sortedRows(
              rows.map((r) => ({ ...r, ...r.metrics })),
              sort,
            )
              .slice((page - 1) * 50, page * 50)
              .map((r) => (
                <tr key={r.id}>
                  <td>
                    {r.machine_name}
                    <small>{shopLabel({ ...r, shop_name: r.name })}</small>
                  </td>
                  <td>
                    {r.duplicate_account
                      ? '重复账号 · 未计入'
                      : !r.online
                        ? '离线 · 上次数据'
                        : !r.supported
                          ? '平台待接入'
                          : r.day_pending || !r.current_day
                            ? '跨日待更新'
                            : r.stale
                              ? '数据待更新'
                              : '已读取'}
                  </td>
                  <td>{money(r.metrics.sales)}</td>
                  <td>{money(r.metrics.orders)}</td>
                  <td>
                    {r.attention ?? '—'}
                    {r.attention_updated && (
                      <small>
                        {new Date(r.attention_updated * 1000).toLocaleTimeString('zh-CN', {
                          timeZone: 'Asia/Shanghai',
                        })}
                      </small>
                    )}
                  </td>
                  <td>{r.operator || '未分配'}</td>
                  <td>
                    {r.updated_at ? new Date(r.updated_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '—'}
                  </td>
                  <td>
                    <NativeButton disabled={!r.online} onClick={() => open(r, 'overview')}>
                      查看经营
                    </NativeButton>{' '}
                    <NativeButton disabled={!r.online} onClick={() => open(r, 'todos')}>
                      重点待办
                    </NativeButton>
                  </td>
                </tr>
              ))}
          </tbody>
        </SortableTable>
        {!rows.length && <p className="table-empty">后台正在汇入摘要，或当前筛选没有店铺。</p>}
      </div>
      <div className="pagination">
        <span>
          第 {page} / {Math.max(1, Math.ceil(rows.length / 50))} 页 · {rows.length} 家
        </span>
        <NativeButton disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
          上一页
        </NativeButton>
        <NativeButton disabled={page * 50 >= rows.length} onClick={() => setPage((p) => p + 1)}>
          下一页
        </NativeButton>
      </div>
    </section>
  );
}
