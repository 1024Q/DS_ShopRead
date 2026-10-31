'use client';
import { NativeButton } from '../shared/AdminControls';
import StatusBadge from '../shared/StatusBadge';
import { useEffect, useRef, useState, useMemo } from 'react';
import { useWorkspace, useDatabaseUpdates, useGlobalRefresh } from '../../providers/WorkspaceContext';
import SortableTable from '../shared/SortableTable';
import { shopLabel } from '../../lib/shop-identity.mjs';
import CollapsibleSection from '../shared/CollapsibleSection';
import { databaseReader } from '../../lib/database-reader';
const stamp = (value) =>
  value ? new Date(value * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '等待首次检查';
export default function AttentionPanel({
  platform = '',
  shopId = '',
  kind = '',
  keyword = '',
  onOpen,
  compact = false,
  onNavigate,
  suppliedData,
}) {
  const readOnly = suppliedData !== undefined;
  const { live, businessDay } = useWorkspace(),
    [savedData, setData] = useState(null),
    [savedError, setError] = useState(''),
    [page, setPage] = useState(1),
    [newCount, setNewCount] = useState(0),
    refresh = useRef(null),
    known = useRef(null);
  const data = readOnly ? suppliedData : savedData,
    error = readOnly ? suppliedData?.error : savedError;
  const [sort, setSort] = useState({ field: 'deadline', direction: 'asc' });
  const read = useMemo(() => databaseReader('/api/attention'), []);
  const query = new URLSearchParams(
    Object.entries({
      sort_by: sort.field,
      sort_direction: sort.direction,
      business_day: businessDay,
      platform,
      shop_id: shopId,
      kind,
      keyword,
      page: String(page),
      summary: compact ? 'true' : '',
    }).filter(([, v]) => v !== '' && v != null),
  ).toString();
  useEffect(() => {
    setPage(1);
    known.current = null;
    setNewCount(0);
  }, [platform, shopId, kind, keyword]);
  useDatabaseUpdates(
    () => {
      if (!readOnly) refresh.current?.();
    },
    {
      kinds: kind ? [kind] : ['aftersales', 'evidence', 'tickets', 'intercepts', 'packages', 'invoices'],
      shopId,
      platform,
    },
  );
  useGlobalRefresh((mode) => {
    if (readOnly) return;
    if (mode !== 'collect') {
      refresh.current?.(false);
      return;
    }
    fetch(
      '/api/attention/sync?' +
        new URLSearchParams(
          Object.entries({ platform, shop_id: shopId, kind }).filter(([, v]) => v !== '' && v != null),
        ),
      { method: 'POST' },
    )
      .then((r) => {
        window.dispatchEvent(new CustomEvent('collection-submitted', { detail: { path: '/api/attention', ok: r.ok } }));
        if (!r.ok) throw Error('重点同步提交失败');
        return refresh.current?.();
      })
      .catch((e) => setError(e.message));
  });
  useEffect(() => {
    if (readOnly) return;
    let active = true,
      busy = false,
      queued = false;
    const c = new AbortController();
    async function load(registerMissing = true) {
      if (busy) {
        queued = true;
        return;
      }
      if (document.hidden) return;
      queued = false;
      busy = true;
      try {
        const r = await read(query, {
          registerMissing,
          signal: c.signal,
          onPreview: (value) => {
            if (active) setData((current) => current || value);
          },
        });
        if (!r.ok) throw Error('重点数据读取失败，将保留上次结果');
        const d = await r.json();
        if (active) {
          const ids = new Set(d.rows.map((r) => `${r.shop_id}:${r.kind}:${r.id}`));
          if (known.current && page === 1) setNewCount([...ids].filter((id) => !known.current.has(id)).length);
          known.current = ids;
          setData(d);
          if (page > d.page_count) setPage(d.page_count);
          setError('');
        }
      } catch (e) {
        if (active && e.name !== 'AbortError') setError(e.message);
      } finally {
        busy = false;
        if (active && queued) queueMicrotask(load);
      }
    }
    refresh.current = load;
    load();
    return () => {
      active = false;
      c.abort();
      refresh.current = null;
    };
  }, [query, page, read, readOnly]);
  if (compact)
    return (
      <NativeButton type="button" className="ov-attention-link" onClick={onNavigate} aria-label="查看重点待处理">
        <span>
          <b>重点待处理</b>
          <small>
            {error
              ? '读取异常 · 保留上次结果'
              : data?.pending
                ? `已读取范围 · ${data.pending} 项待检查`
                : data?.stale
                  ? '部分数据待更新'
                  : `每 ${data?.interval_seconds ?? 60} 秒检查 · 自动更新`}
          </small>
        </span>
        <strong>{data?.total == null ? '—' : data.total.toLocaleString('zh-CN')}</strong>
        <span aria-hidden="true">↗</span>
      </NativeButton>
    );
  return (
    <CollapsibleSection className="data-module attention-panel">
      <div className="trade-detail-heading">
        <h3>
          重点待处理 <small>{data?.total ?? '—'} 条</small>
        </h3>
        <span className="data-note">
          每 {data?.interval_seconds ?? 60} 秒检查 · {live ? '更新自动推送' : '定时刷新兜底'}
        </span>
        {newCount > 0 && <span className="priority-summary">本页新增 {newCount} 条</span>}
      </div>

      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {data?.pending > 0 && <p className="data-note">{data.pending} 个模块待首次更新，数量尚不完整。</p>}
      {data?.stale > 0 && <p className="data-note">{data.stale} 个模块超过配置检查周期，沿用上次结果。</p>}
      <div className="data-table-wrap">
        <SortableTable
          className="data-table"
          serverSort={{
            fields: ['shop_name', 'module_name', 'order_number', 'status', 'deadline', 'updated_at'],
            ...sort,
            onChange: (field, direction) => {
              setSort({ field, direction });
              setPage(1);
            },
          }}
        >
          <thead>
            <tr>
              {['店铺', '重点事项', '订单 / 工单', '状态', '处理期限', '检查时间', '操作'].map((t) => (
                <th key={t}>{t}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(data?.rows || []).map((r) => (
              <tr
                data-shop-id={r.shop_id}
                key={`${r.shop_id}:${r.kind}:${r.id}`}
                className={`priority-row attention-row priority-${r.priority}`}
              >
                <td>{shopLabel(r)}</td>
                <td>{r.module_name}</td>
                <td>
                  {r.order_number || r.id}
                  {r.tracking_number && <small>运单：{r.tracking_number}</small>}
                </td>
                <td>
                  <StatusBadge value={r.priority_reason || r.attention_status || r.status || '待核对'} />
                  {r.error && <small className="error">检查异常，保留上次数据</small>}
                </td>
                <td>{r.deadline || '平台未提供'}</td>
                <td>
                  {stamp(r.updated_at)}
                  {r.stale && <small>上次结果</small>}
                </td>
                <td>
                  {onNavigate && (
                    <NativeButton
                      className="table-action"
                      onClick={() =>
                        onNavigate(r.kind, {
                          shop_id: String(r.shop_id),
                          keyword: r.order_number || String(r.id),
                          priority_only: true,
                          date_from: (r.created_at || '').slice(0, 10),
                          date_to: (r.created_at || '').slice(0, 10),
                          status: '',
                        })
                      }
                    >
                      查看模块
                    </NativeButton>
                  )}
                  <NativeButton className="table-action" onClick={() => onOpen?.(r.shop_id, r.kind)}>
                    查看后台
                  </NativeButton>
                </td>
              </tr>
            ))}
          </tbody>
        </SortableTable>
      </div>
      {data && !data.total && (
        <p className="table-empty">{data.pending ? '重点范围尚未全部读取完成。' : '已读取范围内暂无重点事项。'}</p>
      )}
      <div className="pagination">
        <span>
          第 {page} / {data?.page_count || 1} 页
        </span>
        <NativeButton disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
          上一页
        </NativeButton>
        <NativeButton disabled={!data || page >= data.page_count} onClick={() => setPage((p) => p + 1)}>
          下一页
        </NativeButton>
      </div>
      <details>
        <summary>检查范围与状态</summary>
        <p className="data-note">
          覆盖拼多多：待举证、平台介入待商家处理 /
          平台处理中、2小时内将逾期工单、拦截失败、即将发货-揽收超时、3天内需开票。其他平台不标记重点。
        </p>
        {(data?.sources || []).map((s) => (
          <p className="data-note" key={`${s.shop_id}:${s.kind}`}>
            {shopLabel(s)} · {s.coverage} · {stamp(s.updated_at)}
            {s.syncing ? ' · 正在检查' : ''}
            {s.error ? ' · ' + s.error : ''}
          </p>
        ))}
      </details>
    </CollapsibleSection>
  );
}
