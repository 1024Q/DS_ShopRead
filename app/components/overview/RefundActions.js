'use client';
import { useEffect, useRef, useState } from 'react';
import { Alert, Table } from 'antd';
import AdminDialog from '../shared/AdminDialog';
import { NativeButton, Choice } from '../shared/AdminControls';
import { shopLabel } from '../../lib/shop-identity.mjs';

const states = {
  checking: '正在直接核验',
  retry: '暂缓，自动重试',
  skipped: '未满足退款条件',
  manual: '需要人工核对',
  checked: '已核验',
  submitting: '提交结果待核对',
  accepted: '平台已受理',
  rejected: '平台未接受',
  unknown: '结果待人工核对',
  not_applied: '核对：平台未显示生效',
  changed: '核对：平台状态已变化',
  review: '记录损坏待核对',
};
const money = (value) => (value == null ? '—' : `¥${(value / 100).toFixed(2)}`);
const stamp = (value) => value ? new Date(value * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—';
const columns = [
  {
    title: '订单号 / 售后单',
    dataIndex: 'order_number',
    render: (value, row) => (
      <>
        {value}
        <small>{row.case_id}</small>
      </>
    ),
  },
  { title: '订单金额', dataIndex: 'order_amount_cents', render: money },
  { title: '申请退款', dataIndex: 'refund_amount_cents', render: money },
  { title: '操作结果', dataIndex: 'state', render: (value, row) => <>{states[value] || value}<small>{row.record_type === 'verification' ? '核验记录' : '退款操作记录'}</small></> },
  { title: '处理说明', dataIndex: 'reason' },
  { title: '下次核验', dataIndex: 'retry_at', render: (value, row) => row.state === 'retry' ? stamp(value) : '—' },
  {
    title: '记录时间',
    dataIndex: 'created_at',
    render: (value) =>
      value ? new Date(value * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—',
  },
];

const csvCell = (value) => {
  const text = String(value ?? '');
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
};
function exportCsv(rows, shop) {
  const header = ['订单号', '售后单', '订单金额', '申请退款', '操作结果', '记录类型', '处理说明', '下次核验', '记录时间'];
  const lines = rows.map((r) =>
    [
      r.order_number,
      r.case_id,
      r.order_amount_cents == null ? '' : (r.order_amount_cents / 100).toFixed(2),
      r.refund_amount_cents == null ? '' : (r.refund_amount_cents / 100).toFixed(2),
      states[r.state] || r.state,
      r.record_type === 'verification' ? '核验记录' : '退款操作记录',
      r.reason,
      r.state === 'retry' && r.retry_at ? stamp(r.retry_at) : '',
      r.created_at
        ? new Date(r.created_at * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
        : '',
    ]
      .map(csvCell)
      .join(','),
  );
  const blob = new Blob(['\ufeff' + [header.join(','), ...lines].join('\n')], { type: 'text/csv;charset=utf-8' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `自动退款记录-${shop}.csv`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

export default function RefundActions({ shops }) {
  const [open, setOpen] = useState(false),
    [shop, setShop] = useState(''),
    [stateFilter, setStateFilter] = useState('');
  const [data, setData] = useState(null),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(0);
  const moreRequest = useRef(null);
  const available = shops.filter((s) => s.platform === 'pdd');
  // Server-side pages (newest first, cursor = created_at); older pages load on demand.
  const load = (before, signal, beforeCaseId = '') => {
    const query = new URLSearchParams({ limit: '100', ...(before ? { before: String(before) } : {}) });
    if (stateFilter) query.set('state', stateFilter);
    if (beforeCaseId) query.set('before_case_id', beforeCaseId);
    return fetch(`/api/shops/${shop}/refund-actions?${query}`, { signal }).then(async (r) => {
      if (!r.ok) throw Error('退款记录暂不可用，请确认对应节点已更新并在线');
      return r.json();
    });
  };
  useEffect(() => {
    if (!open || !shop) return;
    const controller = new AbortController();
    setData(null);
    setBusy(true);
    setError('');
    load(0, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setData(value);
      })
      .catch((e) => {
        if (e.name !== 'AbortError') setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => {
      controller.abort();
      moreRequest.current?.abort();
      moreRequest.current = null;
    };
  }, [open, shop, version, stateFilter]);
  useEffect(() => {
    // Keep older loaded pages stable; the first page can show live decisions
    // without clearing the table or displaying a loading overlay each time.
    if (!open || !shop || busy || !data || data.rows.length > 100) return;
    let active;
    const timer = setInterval(async () => {
      if (document.visibilityState !== 'visible' || active || moreRequest.current) return;
      active = new AbortController();
      const request = active;
      try {
        const next = await load(0, request.signal);
        if (!request.signal.aborted) setData(next);
      } catch (e) {
        if (e.name !== 'AbortError') setError(e.message);
      } finally {
        if (active === request) active = null;
      }
    }, 5000);
    return () => { clearInterval(timer); active?.abort(); };
  }, [open, shop, stateFilter, busy, data?.rows?.length]);
  const more = () => {
    if (busy || !data?.has_more) return;
    const controller = new AbortController();
    moreRequest.current = controller;
    setBusy(true);
    load(data?.next_before, controller.signal, data?.next_case_id)
      .then((next) => {
        if (controller.signal.aborted || moreRequest.current !== controller) return;
        setData((old) => ({
          ...next,
          rows: [...(old?.rows || []), ...next.rows],
          pending_review: old?.pending_review,
        }));
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (moreRequest.current === controller) {
          moreRequest.current = null;
          setBusy(false);
        }
      });
  };
  return (
    <>
      <NativeButton
        onClick={() => {
          setShop((s) => s || String(available[0]?.id || ''));
          setOpen(true);
        }}
      >
        自动退款记录
      </NativeButton>
      {open && (
        <AdminDialog className="detail-dialog" onClose={() => setOpen(false)}>
          <div className="dialog-heading">
            <h2>自动退款记录</h2>
            <NativeButton onClick={() => setOpen(false)}>关闭</NativeButton>
          </div>
          <Alert
            type="info"
            showIcon
            message="仅处理平台处理中、订单金额低于 100 元的售后单。需要验货或额外验证时保留人工处理；提交结果不明确时不会自动重提。"
          />
          <div className="filter-panel">
            <label>
              店铺
              <Choice value={shop} onChange={(e) => setShop(e.target.value)}>
                <option value="">选择店铺</option>
                {available.map((s) => (
                  <option value={s.id} key={s.id}>
                    {shopLabel(s)}
                  </option>
                ))}
              </Choice>
            </label>
            <label>
              操作结果
              <Choice value={stateFilter} onChange={(e) => setStateFilter(e.target.value)}>
                <option value="">全部</option>
                {Object.entries(states).map(([value, label]) => (
                  <option value={value} key={value}>
                    {label}
                  </option>
                ))}
              </Choice>
            </label>
            <NativeButton onClick={() => setVersion((v) => v + 1)}>刷新记录</NativeButton>
            <NativeButton disabled={!data?.rows?.length} onClick={() => exportCsv(data.rows, shop)}>
              导出已加载记录
            </NativeButton>
          </div>
          {error && <Alert type="error" message={error} />}
          {data?.pending_review > 0 && (
            <Alert
              type="warning"
              showIcon
              message={`${data.pending_review} 条记录待核对。提交结果不明的仅查询平台回执；要求验货或额外验证的订单保留人工处理。`}
            />
          )}
          <Table
            rowKey="case_id"
            dataSource={data?.rows || []}
            columns={columns}
            loading={busy}
            scroll={{ x: 1250 }}
            pagination={{ pageSize: 20 }}
          />
          {data?.has_more && (
            <NativeButton disabled={busy} onClick={more}>
              加载更早的记录
            </NativeButton>
          )}
        </AdminDialog>
      )}
    </>
  );
}
