'use client';
import SortableTable from '../shared/SortableTable';

import { useEffect, useState } from 'react';
import { shopLabel } from '../../lib/shop-identity.mjs';
import { moduleNames, sourceScope } from '../shell/WorkspaceTools';
export default function QueueDiagnostics({ status }) {
  const [message, setMessage] = useState(''),
    [busy, setBusy] = useState(null);
  const [expanded, setExpanded] = useState(false),
    [page, setPage] = useState(1);
  const [detail, setDetail] = useState(null),
    [loadError, setLoadError] = useState('');
  useEffect(() => {
    if (!expanded) return;
    let alive = true,
      busy = false;
    const c = new AbortController();
    const load = async () => {
      if (busy || document.hidden) return;
      busy = true;
      try {
        const r = await fetch(`/api/system/progress?tasks=true&page=${page}`, { signal: c.signal });
        if (!r.ok) throw Error('任务明细读取失败');
        const d = await r.json();
        if (alive) {
          setDetail(d.diagnostics);
          setLoadError('');
        }
      } catch (e) {
        if (alive && e.name !== 'AbortError') setLoadError(e.message);
      } finally {
        busy = false;
      }
    };
    load();
    const t = setInterval(load, 5000);
    return () => {
      alive = false;
      c.abort();
      clearInterval(t);
    };
  }, [expanded, page]);
  const d = status?.diagnostics,
    q = status?.queues;
  if (!d) return null;
  const total = detail?.task_count ?? d.task_count ?? 0,
    pages = Math.max(1, Math.ceil(total / 20)),
    current = Math.min(page, pages);
  return (
    <div className="queue-diagnostics">
      {message && <p role="status">{message}</p>}
      <div className="queue-health">
        <span>
          最长等待 <b>{Math.ceil((q?.oldest_wait_seconds || 0) / 60)} 分钟</b>
        </span>
        <span>
          处理速度 <b>{d.tasks_per_minute} 项/分钟</b>
        </span>
        <span>
          近 {d.sample_minutes} 分钟完成 <b>{d.success_15m}</b> · 失败 <b>{d.failed_15m}</b>
        </span>
        <span>
          重试占用{' '}
          <b>
            {q?.retry_running || 0} / {q?.retry_limit || 1}
          </b>
        </span>
        <button type="button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
          {expanded ? '收起任务明细' : '查看任务明细'}
        </button>
      </div>
      {expanded && (
        <>
          {loadError && <p className="error">{loadError}</p>}
          <div className="data-table-wrap">
            <SortableTable className="data-table">
              <thead>
                <tr>
                  {[
                    '店铺 / 模块',
                    '采集队列',
                    '状态',
                    '等待 / 耗时',
                    '原因 / 当前步骤',
                    '采集范围',
                    '生成 / 最近合并',
                  ].map((x) => (
                    <th key={x}>{x}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(detail?.tasks || []).map((t, i) => (
                  <tr key={`${t.shop_id}:${t.kind}:${i}`}>
                    <td>
                      {shopLabel(t)}
                      <small>{moduleNames[t.kind] || t.kind}</small>
                    </td>
                    <td>
                      <span>{t.frequency_name || t.queue}</span>
                    </td>
                    <td>
                      {t.phase === 'running' ? '读取中' : '排队中'}
                      {t.retry ? ' · 重试' : ''}
                    </td>
                    <td>
                      {t.phase === 'running' ? t.elapsed_seconds : t.wait_seconds} 秒
                      {t.phase === 'running' && <small>超时剩余 {t.remaining_seconds ?? '—'} 秒</small>}
                    </td>
                    <td>
                      {t.wait_reason || t.stage || '准备读取'}
                      {t.blocking_task && <small>占用范围：{sourceScope(t.blocking_task)}</small>}
                      {t.last_duration_seconds != null && <small>上次耗时 {t.last_duration_seconds} 秒</small>}
                    </td>
                    <td>{sourceScope(t)}</td>
                    <td>
                      {t.generated_at ? new Date(t.generated_at * 1000).toLocaleString('zh-CN') : '—'}
                      <small>
                        {t.last_merged_at
                          ? '最近合并 ' + new Date(t.last_merged_at * 1000).toLocaleString('zh-CN')
                          : '未合并'}
                      </small>
                      {t.overdue_seconds > 0 && <small className="error">排队超期 {t.overdue_seconds} 秒</small>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </SortableTable>
          </div>
          <div className="pagination">
            <span>
              {total} 项任务 · 第 {current} / {pages} 页
            </span>
            <button disabled={current === 1} onClick={() => setPage(current - 1)}>
              上一页
            </button>
            <button disabled={current === pages} onClick={() => setPage(current + 1)}>
              下一页
            </button>
          </div>
        </>
      )}
    </div>
  );
}
