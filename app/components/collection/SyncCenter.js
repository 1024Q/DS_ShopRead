'use client';
import { ActionButton, TextInput, Choice } from '../shared/AdminControls';
import { formatInterval, groupSourceRows } from '../../lib/source-display.mjs';
import { filterSources, sourceFilterHelp } from '../../lib/source-filter.mjs';
import MultiSelect from '../shared/MultiSelect';
import usePersistentSelection from '../../hooks/usePersistentSelection';
import { inScope } from '../../lib/filter-scope.mjs';
import { throttleUpdates, updateMatches } from '../../lib/data-updates.mjs';
import UpdatedAgo from '../shared/UpdatedAgo';
import { shopLabel } from '../../lib/shop-identity.mjs';
import { Drawer } from 'antd';
import CollectionManagement from './CollectionManagement';
import SortableTable from '../shared/SortableTable';

import { Fragment, memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  sourceIssue as issue,
  sourceActive as active,
  retrySourceGroup,
  retrySources,
} from '../../lib/source-retry.mjs';
import { useSyncStatus } from '../../providers/WorkspaceContext';
import { beijing, moduleNames, sourceScope } from '../shell/WorkspaceTools';
import { shopStateLabels } from '../../lib/fetch-json';
const labels = {
  stopped: '店铺已停止',
  deferred: '待调度（限流）',
  blocked: '暂停重试',
  paused_batch: '断点续采',
  queued: '排队中',
  running: '读取中',
  waiting_login: shopStateLabels.needs_verification,
  error: '读取失败',
  missing: '尚未读取',
  partial: '部分读取',
  ready: '读取完成',
};
// One status control. Keys are the backend 'state' filter values; counts come from listing.counts, where the backend names the 'recorded' total 'ready'.
const statusFilters = [
  ['', '全部数据源', 'all'],
  ['active', '同步 / 排队', 'active'],
  ['issues', '异常 / 待验证', 'issues'],
  ['recorded', '已有记录', 'ready'],
];
function QueueLabel({ source: s }) {
  const queue = s.queue || (s.scope?._attention ? 'focus' : 'normal');
  return (
    <span className="sync-state muted">
      {s.queue_names?.[queue] || { focus: '实时采集', normal: '常规采集', daily: '每日采集' }[queue] || queue}
    </span>
  );
}
function RetryCountdown({ source: s, online = true }) {
  const [now, setNow] = useState(s.retry_server_time || Date.now() / 1000);
  useEffect(() => {
    const base = s.retry_server_time || Date.now() / 1000,
      started = Date.now();
    setNow(base);
    const timer = setInterval(() => setNow(base + (Date.now() - started) / 1000), 1000);
    return () => clearInterval(timer);
  }, [s.retry_server_time]);
  const deadline = s.repair_retry_at ?? s.retry_at;
  if (!online) return <small className="sync-retry-count">采集服务离线，恢复后继续重试</small>;
  if (s.state === 'stopped') return null;
  if (s.waiting_login || s.state !== 'logged_in')
    return <small className="sync-retry-count">等待登录验证，完成后恢复</small>;
  if (deadline == null)
    return s.repair_manual_count > 0 ? (
      <small className="sync-retry-count">{s.repair_manual_count} 个范围需处理权限或适配问题后重试</small>
    ) : null;
  const remaining = Math.max(0, Math.ceil(deadline - now));
  return (
    <small className="sync-retry-count" role="timer" aria-live="off">
      {remaining
        ? `自动重试倒计时 ${Math.floor(remaining / 60)}分${String(remaining % 60).padStart(2, '0')}秒`
        : '重试已到期 · 等待调度 / 可用名额'}
      <br />
      计划重试：{beijing(new Date(deadline * 1000).toISOString())}
      {s.repair_auto_count > 1 ? ` · ${s.repair_auto_count} 个范围自动恢复` : ''}
      {s.repair_manual_count > 0 ? ` · ${s.repair_manual_count} 个范围需人工处理` : ''}
    </small>
  );
}
function FailureHistory({ source: s, online }) {
  const count = Math.max(s.failures || 0, s.failed_scope_failures || 0);
  if (!count && !s.previous_error && !s.repair_advice?.length && !s.error) return null;
  return (
    <section className="source-failure-history">
      <strong>历史失败与恢复计划</strong>
      {count > 0 && (
        <small>
          失败范围累计连续失败最多 {count} 次 · 已重试 {Math.max(0, count - 1)} 次
        </small>
      )}
      {active(s) && <small>以下为此前失败记录，不代表当前排队或读取失败；重试计划针对失败范围。</small>}
      {(s.previous_error || s.error) && <small className="error">上次异常：{s.previous_error || s.error}</small>}
      {s.repair_advice?.length > 0 && (
        <details className="repair-advice">
          <summary>历史失败原因与处理方法</summary>
          {s.repair_advice.map((a, i) => (
            <div key={i}>
              <strong>{a.reason}</strong>
              <small>{a.action}</small>
            </div>
          ))}
        </details>
      )}
      {issue(s) && <RetryCountdown source={s} online={online} />}
    </section>
  );
}
function DeliveryStatus({ source: s, details = false }) {
  const d = s.delivery;
  if (!d) return <small>节点升级后显示上传状态</small>;
  const names = {
    unpublished: '尚未入库',
    checkpoint: '断点已保存 · 尚未上传',
    pending: '本地已入库 · 待上传',
    uploading: '上传中',
    failed: '上传失败',
    confirmed: '中心已确认',
  };
  const stamp = (v) => (v ? beijing(new Date(v * 1000).toISOString()) : '—');
  return (
    <div className="source-delivery">
      <small className={d.state === 'failed' ? 'error' : ''} title={d.error || ''}>
        {names[d.state] || '等待上传状态'}
      </small>
      {details && (
        <>
          <small>本地入库：{stamp(d.local_at)}</small>
          <small>中心确认：{stamp(d.confirmed_at)}</small>
          {d.retry_at && (
            <small>
              上传重试：{stamp(d.retry_at)} · 已失败 {d.attempts || 0} 次
            </small>
          )}
          {d.error && <small className="error">{d.error}</small>}
          <small>
            当前已读取：{s.sync_loaded || s.checkpoint_loaded || s.loaded_count || 0} /{' '}
            {s.sync_total ?? s.checkpoint_total ?? s.source_total ?? '未知'}
          </small>
        </>
      )}
    </div>
  );
}
function SourceFailures({ source }) {
  const [detail, setDetail] = useState(null),
    [error, setError] = useState('');
  const count = source.failed_scope_count ?? source.failed_scopes?.length ?? 0;
  if (!count) return null;
  return (
    <details
      onToggle={async (e) => {
        if (!e.currentTarget.open) return;
        setDetail(null);
        setError('');
        try {
          const r = await fetch(
            '/api/system/sources?' +
              new URLSearchParams({
                shop_id: String(source.shop_id),
                kind: source.kind,
                cadence: source.queue || '',
                details: 'true',
              }),
          );
          if (!r.ok) throw Error('诊断暂不可用');
          const d = await r.json();
          setDetail(d.rows?.[0]?.failed_scopes || []);
        } catch (e) {
          setError(e.message);
        }
      }}
    >
      <summary>失败范围 {count} 项</summary>
      {error ||
        (!detail
          ? '正在读取诊断…'
          : detail.map((f) => (
              <small key={f.cache_key}>
                {sourceScope({ ...source, scope: f.scope })} · 失败 {f.failures || 0} 次{f.error ? ' · ' + f.error : ''}
              </small>
            )))}
    </details>
  );
}
const SourceDetailRow = memo(function SourceDetailRow({
  source: s,
  onOpen,
  batchBusy,
  showOperators,
  onRetried,
  collectorOnline,
}) {
  const [busy, setBusy] = useState(false),
    [message, setMessage] = useState('');
  async function retry() {
    setBusy(true);
    setMessage('');
    try {
      await retrySourceGroup(s);
      setMessage('已进入重试队列');
      onRetried?.(s);
    } catch (e) {
      setMessage(e.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <tr className={active(s) && s.scope?._attention ? 'sync-priority-row' : undefined}>
      <td>
        <strong>{shopLabel(s)}</strong>
      </td>
      {showOperators && <td>{(s.operators || []).join('、') || '—'}</td>}
      <td>
        {s.task_label || moduleNames[s.kind] || s.kind}
        <small className={s.scope?._attention ? 'sync-priority-label' : undefined}>
          {s.scope?._exact_dates
            ? '指定日期 · 单次采集'
            : s.cadence === 'off'
              ? '已暂停'
              : s.cadence === 'realtime'
                ? `${s.frequency_name || (s.scope?._attention ? '重点优先检查' : s.custom_interval ? '自定义采集间隔' : '实时定时采集')}${s.interval_seconds ? ' · 每 ' + formatInterval(s.interval_seconds) : ''}`
                : s.frequency_name || '每日采集'}
        </small>
      </td>
      <td>
        <span className={`sync-state ${active(s) ? 'active' : issue(s) ? 'issue' : s.updated_at ? 'ready' : 'muted'}`}>
          {labels[s.data_status] || '等待同步'}
        </span>
        {s.stale && s.updated_at && <small>保留上次数据</small>}
      </td>
      <td className="sync-detail">
        <strong>当前任务</strong>
        {s.state === 'stopped' ? (
          <>店铺已停止，启动后恢复采集</>
        ) : !active(s) && s.error ? (
          <>
            <span className="error">{s.error}</span>
            <small>
              {{
                network: '网络 / 浏览器超时 · ',
                permission: '权限不足 · ',
                adapter: '页面适配变化 · ',
                unsupported: '范围暂不支持 · ',
                login: '登录失效 · ',
                transient: '临时异常 · ',
              }[s.error_kind] || ''}
              {s.waiting_login
                ? '完成登录验证后恢复'
                : s.blocked
                  ? s.error_kind === 'permission'
                    ? '需开通官方权限后手动重试'
                    : s.error_kind === 'unsupported'
                      ? '请选择受支持的日期范围'
                      : '需检查适配或所选范围后手动重试'
                  : s.retry_at
                    ? '按计划自动重试'
                    : s.retry_enabled
                      ? '等待店铺恢复后自动重试'
                      : '自动采集已暂停'}
              {s.failures ? ` · 连续失败 ${s.failures} 次` : ''}
            </small>
          </>
        ) : s.waiting_login ? (
          '请打开店铺后台完成验证'
        ) : active(s) ? (
          <>
            <span>{s.sync_phase === 'queued' ? s.wait_reason || '等待可用浏览器' : s.sync_stage || '读取中'}</span>
            {s.sync_phase === 'queued' && <small>已等待 {s.wait_seconds ?? 0} 秒</small>}
            {s.sync_phase !== 'queued' && (
              <small>
                已读取 {s.sync_loaded || 0}
                {s.sync_total != null ? ' / ' + s.sync_total : ''} · {s.elapsed_seconds ?? 0} 秒
              </small>
            )}
            {s.sync_total > 0 && <progress max={s.sync_total} value={s.sync_loaded || 0} />}
          </>
        ) : s.updated_at ? (
          <>
            {s.details_on_demand
              ? '首页摘要已更新 · 明细按需采集'
              : s.kind === 'overview'
                ? '经营指标快照'
                : `${s.loaded_count} 条记录 / 平台 ${s.source_total ?? '未知'} ${s.source_unit || '条'}`}
            <small>{s.duration_seconds != null ? `上次耗时 ${s.duration_seconds} 秒` : '已保存到数据库'}</small>
          </>
        ) : (
          '等待首次采集'
        )}
        <FailureHistory source={s} online={collectorOnline} />
        {s.deferred && <small>到期需求已保留，按处理能力分批入队；现有数据继续展示</small>}
        {s.continuation && (
          <small>
            断点已存：{s.checkpoint_loaded} / {s.checkpoint_total ?? '未知'}，完成整批范围后更新正式数据
          </small>
        )}
      </td>
      <td>
        <SourceFailures source={s} />
        {sourceScope(s)}
        {s.scope?.status && <small>{s.scope.status}</small>}
      </td>
      <td className="date-cell">
        <UpdatedAgo value={s.updated_at} />
        <DeliveryStatus source={s} details />
        <small className="freshness-badge">
          {s.stale ? '待更新' : s.details_on_demand ? '首页摘要已更新' : s.complete === false ? '部分数据' : '已入库'}
        </small>
        {s.details_pending && (
          <small>
            明细：{s.details_updated_at ? beijing(s.details_updated_at) : '尚未完整采集'}
            {s.details_on_demand ? ' · 按需采集' : ' · 待完成'}
          </small>
        )}
        <small>平台：{s.source_time || '未提供'}</small>
        {s.collection_mode && (
          <small>
            {s.collection_mode === 'summary'
              ? '首页摘要读取'
              : s.collection_mode === 'attention'
                ? '独立重点快照'
                : s.collection_mode === 'incremental'
                  ? '增量更新'
                  : '完整读取'}
            {s.full_checked_at ? ' · 完整核对 ' + beijing(s.full_checked_at) : ''}
          </small>
        )}
      </td>
      <td>
        <ActionButton
          className="table-action"
          onClick={() => onOpen(s.shop_id, s.kind === 'tax' ? 'reconciliation_month' : s.kind)}
        >
          查看后台
        </ActionButton>{' '}
        <ActionButton
          className="table-action"
          disabled={
            busy || batchBusy || (active(s) && !s.failed_scope_count) || s.waiting_login || s.state === 'stopped'
          }
          onClick={retry}
        >
          {busy ? '提交中…' : issue(s) ? '重试' : '采集此范围'}
        </ActionButton>
        {message && <small role="status">{message}</small>}
      </td>
    </tr>
  );
});
function SourceDialog({ source, onClose, ...props }) {
  return (
    <Drawer open onClose={onClose} title="采集详情" size="large" styles={{ wrapper: { maxWidth: '100vw' } }}>
      <div className="source-dialog-body">
        <header>
          <div>
            <h3>{moduleNames[source.kind] || source.kind} · 采集详情</h3>
            <p>{shopLabel(source)}</p>
          </div>
          <ActionButton onClick={onClose} aria-label="关闭采集详情">
            关闭 ×
          </ActionButton>
        </header>
        <table className="sync-table source-detail-table">
          <tbody>
            <SourceDetailRow source={source} {...props} />
          </tbody>
        </table>
      </div>
    </Drawer>
  );
}
const SourceRow = memo(
  function SourceRow({ source: s, onOpen, batchBusy, showOperators, onRetried, collectorOnline }) {
    const [expanded, setExpanded] = useState(false);
    const failures = Math.max(s.failures || 0, s.failed_scope_failures || 0);
    const stopped = s.state === 'stopped',
      isIssue = issue(s),
      isActive = active(s);
    const summary = stopped
      ? '启动店铺后恢复采集'
      : isActive
        ? s.sync_phase === 'queued'
          ? s.wait_reason || '等待可用名额'
          : s.sync_stage || '读取中'
        : s.error ||
          s.wait_reason ||
          s.sync_stage ||
          (s.details_on_demand
            ? '首页摘要已更新 · 明细按需采集'
            : s.updated_at
              ? `${s.loaded_count ?? 0} 条已保存`
              : '等待首次采集');
    return (
      <>
        <tr className={isActive && s.scope?._attention ? 'sync-priority-row' : undefined}>
          <td>
            <span className="source-ellipsis" title={shopLabel(s)}>
              {shopLabel(s)}
            </span>
          </td>
          {showOperators && (
            <td>
              <span className="source-ellipsis" title={(s.operators || []).join('、')}>
                {(s.operators || []).join('、') || '—'}
              </span>
            </td>
          )}
          <td>
            <strong>{moduleNames[s.kind] || s.kind}</strong>
            <small className={s.scope?._attention ? 'sync-priority-label' : ''}>
              {s.frequency_name || '默认采集'}
              {s.interval_seconds ? ` · 每 ${formatInterval(s.interval_seconds)}` : ''}
            </small>
          </td>
          <td>
            <QueueLabel source={s} />
          </td>
          <td>
            <span
              className={`sync-state ${stopped ? 'muted' : isActive ? 'active' : isIssue ? 'issue' : s.updated_at ? 'ready' : 'muted'}`}
            >
              {labels[s.data_status] || '等待同步'}
            </span>
            {!stopped && failures > 0 && <small className="sync-retry-count">历史失败 {failures} 次 · 查看详情</small>}
          </td>
          <td>
            <span className={`source-ellipsis ${isIssue && !isActive ? 'error' : ''}`} title={summary}>
              {summary}
            </span>
            {isActive && s.sync_total > 0 && <progress max={s.sync_total} value={s.sync_loaded || 0} />}
          </td>
          <td>
            <span className="source-ellipsis" title={sourceScope(s)}>
              {sourceScope(s)}
            </span>
          </td>
          <td>
            <UpdatedAgo value={s.updated_at} />
            <DeliveryStatus source={s} />
            {s.stale && <small>待更新</small>}
          </td>
          <td>
            <div className="source-row-actions">
              <ActionButton className="table-action" onClick={() => setExpanded(true)}>
                详情{isIssue ? ' / 重试' : ''}
              </ActionButton>
              <ActionButton
                className="table-action"
                onClick={() => onOpen(s.shop_id, s.kind === 'tax' ? 'reconciliation_month' : s.kind)}
              >
                后台 ↗
              </ActionButton>
            </div>
          </td>
        </tr>
        {expanded && (
          <tr className="source-dialog-host">
            <td colSpan={showOperators ? 9 : 8}>
              <SourceDialog
                source={s}
                onClose={() => setExpanded(false)}
                onOpen={onOpen}
                batchBusy={batchBusy}
                showOperators={showOperators}
                onRetried={onRetried}
                collectorOnline={collectorOnline}
              />
            </td>
          </tr>
        )}
      </>
    );
  },
  (a, b) =>
    a.onOpen === b.onOpen &&
    a.onRetried === b.onRetried &&
    a.batchBusy === b.batchBusy &&
    a.showOperators === b.showOperators &&
    a.collectorOnline === b.collectorOnline &&
    JSON.stringify(a.source) === JSON.stringify(b.source),
);
function SourceInventory({ shops, onOpen }) {
  const { status, statusError } = useSyncStatus();
  const openRef = useRef(onOpen);
  openRef.current = onOpen;
  const openSource = useCallback((...args) => openRef.current?.(...args), []);
  const [sort, setSort] = useState({});
  const batchLock = useRef(false),
    [batchBusy, setBatchBusy] = useState(false),
    [batchResult, setBatchResult] = useState(null);
  const [cadence, setCadence] = useState('');
  const [shop, setShop] = usePersistentSelection('sources-shops'),
    [kind, setKind] = useState(''),
    [state, setState] = useState(''),
    [keyword, setKeyword] = useState(''),
    [page, setPage] = useState(1);
  const [snapshot, setSnapshot] = useState(null),
    [sourceError, setSourceError] = useState('');
  const params = new URLSearchParams({
    shop_id: shop,
    kind,
    cadence,
    state,
    keyword,
    sort_by: sort.field || '',
    sort_direction: sort.direction || 'asc',
    page: String(page),
  }).toString();
  const complete = !!snapshot?.snapshot_complete;
  const requestKey = complete ? 'snapshot' : params;
  const shopScope = useRef(shop);
  shopScope.current = shop;
  const lastSources = useRef('');
  useEffect(() => {
    let active = true,
      busy = false,
      queued = false;
    const controller = new AbortController();
    const load = async () => {
      if (!active || document.hidden) return;
      if (busy) {
        queued = true;
        return;
      }
      queued = false;
      busy = true;
      try {
        // Compare the raw response text: an unchanged refresh skips JSON parsing,
        // deep comparison and the table re-render entirely.
        const fetchRows = async (query) => {
          const r = await fetch('/api/system/sources?' + query, { signal: controller.signal });
          if (!r.ok) throw Error('数据源暂不可用');
          return r.text();
        };
        let text = await fetchRows('snapshot=true');
        if (text !== lastSources.current) {
          let d = JSON.parse(text);
          if (!d.snapshot_complete) {
            text = await fetchRows(params); // older nodes continue server-side pagination until upgraded
            d = text === lastSources.current ? null : JSON.parse(text);
          }
          if (active && d) {
            lastSources.current = text;
            setSnapshot(d);
          }
        }
        if (active) {
          setSourceError('');
        }
      } catch (e) {
        if (active && e.name !== 'AbortError') setSourceError(e.message);
      } finally {
        busy = false;
        if (queued && active) throttled();
      }
    };
    // Uploads already push 'database-updated' over SSE; the timer is only a fallback.
    // Events for other shops are ignored and bursts are merged (≤ 1 load / 3 s).
    const throttled = throttleUpdates(load, 3000);
    throttled();
    const changed = (e) => {
      if (updateMatches(e.detail, { shopId: shopScope.current })) throttled();
    };
    const timer = setInterval(throttled, 15000);
    document.addEventListener('visibilitychange', throttled);
    window.addEventListener('database-updated', changed);
    return () => {
      active = false;
      controller.abort();
      clearInterval(timer);
      throttled.cancel();
      document.removeEventListener('visibilitychange', throttled);
      window.removeEventListener('database-updated', changed);
    };
  }, [requestKey]);
  const listing = useMemo(
    () =>
      snapshot?.snapshot_complete
        ? filterSources(snapshot, { shop, kind, cadence, state, keyword, sort, page })
        : snapshot,
    [snapshot, shop, kind, cadence, state, keyword, sort, page],
  );
  const showOperators = useMemo(() => shops.some((s) => s.operators?.length), [shops]);
  const onRetried = useCallback((source) => {
    setSnapshot((old) =>
      old
        ? {
            ...old,
            rows: old.rows.map((r) =>
              r.shop_id === source.shop_id && r.kind === source.kind && r.queue === source.queue
                ? {
                    ...r,
                    error: null,
                    failed_scopes: [],
                    failed_scope_count: 0,
                    blocked: false,
                    waiting_login: false,
                    syncing: true,
                    data_status: 'queued',
                  }
                : r,
            ),
          }
        : old,
    );
  }, []);
  const kinds = listing?.kinds || [],
    counts = listing?.counts || { all: 0, issues: 0, active: 0, ready: 0 };
  const pages = listing?.page_count || 1,
    current = listing?.page || 1,
    rows = listing?.rows || [],
    total = listing?.total || 0;
  const retryCount = listing?.retry_count || 0,
    waiting = listing?.waiting || 0;
  async function retryIssues() {
    if (batchLock.current) return;
    batchLock.current = true;
    setBatchBusy(true);
    setBatchResult({ total: retryCount, submitted: 0, completed: 0, failed: [] });
    try {
      const r = await fetch('/api/system/sources?' + params + '&retry=true');
      if (!r.ok) throw Error('无法读取失败范围');
      const d = await r.json();
      await retrySources(d.rows, setBatchResult);
    } catch (e) {
      setSourceError(e.message);
    } finally {
      batchLock.current = false;
      setBatchBusy(false);
    }
  }
  const change = (setter, value) => {
    setter(value);
    setPage(1);
  };
  if (!status || !listing)
    return (
      <section className="business-panel sync-center">
        <h2>同步与数据源</h2>
        <p role="status">{sourceError || statusError || '正在读取详细同步状态…'}</p>
      </section>
    );
  return (
    <section className="business-panel sync-center">
      <h3>数据源与上传明细</h3>
      <div className="sync-overview" role="group" aria-label="同步状态筛选">
        {statusFilters.map(([value, label, key]) => (
          <ActionButton
            key={value || 'all'}
            className={`${state === value ? 'selected' : ''}${key ? '' : ' sync-overview-detail'}`}
            title={sourceFilterHelp[key]}
            aria-pressed={state === value}
            onClick={() => change(setState, value)}
          >
            <span>{label}</span>
            {key && <strong>{counts[key] ?? 0}</strong>}
          </ActionButton>
        ))}
      </div>
      <p className="data-note">{sourceFilterHelp[statusFilters.find(([value]) => value === state)?.[2] || 'all']}</p>
      <div className="filter-panel">
        <label>
          采集队列
          <Choice aria-label="采集队列筛选" value={cadence} onChange={(e) => change(setCadence, e.target.value)}>
            <option value="">全部队列</option>
            {(status?.collection?.frequency_groups || []).map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
            <option value="off">已暂停</option>
          </Choice>
        </label>
        <label>
          店铺
          <MultiSelect
            label="数据源店铺"
            value={shop}
            onChange={(value) => change(setShop, value)}
            options={shops.map((s) => ({ value: s.id, label: shopLabel(s) }))}
            placeholder="全部店铺"
          />
        </label>
        <label>
          业务模块
          <Choice aria-label="数据源模块" value={kind} onChange={(e) => change(setKind, e.target.value)}>
            <option value="">全部模块</option>
            {kinds.map((k) => (
              <option key={k} value={k}>
                {moduleNames[k] || k}
              </option>
            ))}
          </Choice>
        </label>
        <label>
          搜索
          <TextInput
            aria-label="数据源搜索"
            placeholder="店铺 / 运营 / 模块 / 错误原因"
            value={keyword}
            onChange={(e) => change(setKeyword, e.target.value)}
          />
        </label>
      </div>
      {(sourceError || statusError) && (
        <p className="error" role="alert">
          {sourceError || statusError}，暂保留上次状态。
        </p>
      )}
      {listing.machine_errors?.length > 0 && (
        <p role="status" className="data-note">
          部分节点暂不可用：{listing.machine_errors.map((e) => e.name).join('、')}
        </p>
      )}
      <div className="sync-retry-toolbar">
        <span className="source-total">{total} 项数据源 · 每页 30 项</span>
        <div>
          <strong>异常处理</strong>
          <span>
            当前筛选可重采 {retryCount} 项{waiting > 0 ? ` · ${waiting} 项需先完成登录验证` : ''}
          </span>
        </div>
        <ActionButton
          className="primary"
          disabled={batchBusy || !retryCount || status?.collector_online === false}
          onClick={retryIssues}
        >
          {batchBusy
            ? `正在提交 ${batchResult?.completed || 0} / ${batchResult?.total || 0}`
            : `重试失败范围（${retryCount}）`}
        </ActionButton>
      </div>
      {batchResult && (
        <div className="sync-retry-result" role="status">
          {batchBusy ? '提交中' : '本次提交完成'}：已提交 {batchResult.submitted} 项，失败 {batchResult.failed.length}{' '}
          项。{!batchBusy && '采集进度随队列状态自动更新。'}
          {batchResult.failed.length > 0 && (
            <details>
              <summary>查看提交失败原因</summary>
              {batchResult.failed.map(({ source, message }, i) => (
                <p key={i}>
                  {shopLabel(source)} · {moduleNames[source.kind] || source.kind}：{message}
                </p>
              ))}
            </details>
          )}
        </div>
      )}

      <div className="data-table-wrap">
        <SortableTable
          className="data-table sync-table sync-compact-table"
          virtualize={false}
          serverSort={{
            fields: [
              'shop_name',
              ...(showOperators ? ['operators'] : []),
              'kind',
              'queue',
              'data_status',
              'loaded_count',
              'scope',
              'updated_at',
            ],
            ...sort,
            onChange: (field, direction) => {
              setSort({ field, direction });
              setPage(1);
            },
          }}
        >
          <thead>
            <tr>
              {[
                '店铺',
                ...(showOperators ? ['运营'] : []),
                '业务模块',
                '采集队列',
                '状态',
                '进度 / 结果',
                '查询范围',
                '最近采集',
                '操作',
              ].map((label) => (
                <th key={label}>{label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {groupSourceRows(rows, status?.collection?.frequency_groups).map((group) => (
              <Fragment key={group.id}>
                <tr className="source-queue-heading">
                  <th colSpan={showOperators ? 9 : 8} scope="colgroup">
                    {group.name} · 本页 {group.rows.length} 项
                  </th>
                </tr>
                {group.rows.map((s) => (
                  <SourceRow
                    key={`${s.shop_id}:${s.kind}:${s.queue}`}
                    source={s}
                    collectorOnline={status?.collector_online !== false}
                    onRetried={onRetried}
                    onOpen={openSource}
                    batchBusy={batchBusy}
                    showOperators={showOperators}
                  />
                ))}
              </Fragment>
            ))}
          </tbody>
        </SortableTable>
        {!rows.length && <div className="table-empty">{status ? '当前筛选没有匹配数据源' : '正在读取同步状态…'}</div>}
      </div>
      <div className="pagination">
        <span>
          第 {current} / {pages} 页 · 共 {total} 项
        </span>
        <ActionButton disabled={current <= 1} onClick={() => setPage(current - 1)}>
          上一页
        </ActionButton>
        <ActionButton disabled={current >= pages} onClick={() => setPage(current + 1)}>
          下一页
        </ActionButton>
      </div>
    </section>
  );
}

export default function SyncCenter(props) {
  return <CollectionManagement {...props} inventory={<SourceInventory shops={props.shops} onOpen={props.onOpen} />} />;
}
