'use client';
import { useEffect, useState } from 'react';
import SortableTable from '../shared/SortableTable';
import { shopLabel } from '../../lib/shop-identity.mjs';
import { moduleNames } from '../shell/WorkspaceTools';
import { useDatabaseUpdates } from '../../providers/WorkspaceContext';
const labels = { new: '新增', changed: '状态变化', resolved: '已解除' };
export default function NotificationHistory({ onNavigate, platform = '', filters = {} }) {
  const [page, setPage] = useState(1),
    [sort, setSort] = useState({ field: 'created', direction: 'desc' });
  const [data, setData] = useState(null),
    [change, setChange] = useState(''),
    [before, setBefore] = useState(0),
    [error, setError] = useState(''),
    [version, setVersion] = useState(0);
  useDatabaseUpdates(
    () => {
      if (!before) setVersion((v) => v + 1);
    },
    {
      kinds: ['aftersales', 'evidence', 'tickets', 'intercepts', 'packages', 'invoices'],
      platform,
      shopId: filters.shop_id,
    },
  );
  useEffect(() => {
    setBefore(0);
    setPage(1);
  }, [platform, filters.shop_id, filters.kind, filters.keyword]);
  useEffect(() => {
    let alive = true;
    const c = new AbortController();
    fetch(
      '/api/attention/history?' +
        new URLSearchParams({
          page: String(page),
          sort_by: sort.field,
          sort_direction: sort.direction,
          before: String(before),
          change,
          platform,
          ...Object.fromEntries(Object.entries(filters).filter(([, v]) => v)),
        }),
      { signal: c.signal, cache: 'no-store' },
    )
      .then(async (r) => {
        const d = await r.json();
        if (!r.ok) throw Error(d.detail || '通知记录读取失败');
        if (alive) {
          setData(d);
          setError('');
        }
      })
      .catch((e) => {
        if (alive && e.name !== 'AbortError') setError(e.message);
      });
    return () => {
      alive = false;
      c.abort();
    };
  }, [page, sort, before, change, version, platform, filters.shop_id, filters.kind, filters.keyword]);
  return (
    <section className="settings-card">
      <div className="section-head">
        <h3>重点变化记录</h3>
        <select
          aria-label="通知类型"
          value={change}
          onChange={(e) => {
            setChange(e.target.value);
            setBefore(0);
            setPage(1);
          }}
        >
          <option value="">全部变化</option>
          {Object.entries(labels).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>
      </div>
      {error && <p className="error">{error}</p>}
      <div className="data-table-wrap">
        <SortableTable
          className="data-table"
          serverSort={{
            fields: ['created', 'shop_name', 'change', 'order_number'],
            ...sort,
            onChange: (field, direction) => {
              setSort({ field, direction });
              setPage(1);
            },
          }}
        >
          <thead>
            <tr>
              <th>时间</th>
              <th>店铺 / 模块</th>
              <th>变化</th>
              <th>订单 / 工单</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {data?.rows.map((r) => (
              <tr key={r.id} data-shop-id={r.shop_id} className={r.change === 'resolved' ? 'resolved-change-row' : ''}>
                <td>{new Date(r.created * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}</td>
                <td>
                  {shopLabel(r)}
                  <small>{moduleNames[r.kind]}</small>
                </td>
                <td>
                  <span className={r.change === 'resolved' ? 'resolved-change-badge' : ''}>{labels[r.change]}</span>
                  <small>{r.context.status || r.context.attention_status}</small>
                </td>
                <td>{r.context.order_number || r.record_id}</td>
                <td>
                  <button
                    onClick={() =>
                      onNavigate(r.kind, {
                        shop_id: String(r.shop_id),
                        keyword: r.context.order_number || r.record_id,
                        status: '',
                        priority_only: r.change !== 'resolved',
                      })
                    }
                  >
                    定位事项 ↗
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </SortableTable>
      </div>
      {data && !data.rows.length && <p>当前没有变化记录。首次导入只建立基线。</p>}
      <div className="pagination">
        <span>
          第 {page} / {data?.page_count || 1} 页 · {data?.total || 0} 条
        </span>
        <button disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
          上一页
        </button>
        <button disabled={page >= (data?.page_count || 1)} onClick={() => setPage((p) => p + 1)}>
          下一页
        </button>
      </div>
    </section>
  );
}
