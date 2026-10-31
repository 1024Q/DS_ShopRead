'use client';
import SortableTable from '../shared/SortableTable';
import { useState } from 'react';
import { shopLabel } from '../../lib/shop-identity.mjs';
import { useI18n } from '../../i18n/I18nProvider';
import { waitText } from '../../lib/queue-wait.mjs';
import TaskTiming from './TaskTiming';
const states = { running: '执行中', queued: '排队中', deferred: '等待执行', waiting: '等待下次执行' };
const stamp = (value) =>
  value ? new Date(value * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '尚未成功';
export default function MasterTasks({ rows }) {
  const { t } = useI18n();
  const [filter, setFilter] = useState(''),
    [page, setPage] = useState(1);
  if (!rows) return null;
  const filtered = rows
      .filter((r) => !filter || r.phase === filter)
      // Queued tasks in execution order first, then the rest as before.
      .sort((a, b) => (a.queue_position ?? Infinity) - (b.queue_position ?? Infinity)),
    pages = Math.max(1, Math.ceil(filtered.length / 20)),
    current = Math.min(page, pages);
  const shops = new Map();
  for (const row of rows) {
    const shop = shops.get(row.shop_id) || { name: shopLabel(row), seconds: {}, masters: 0 };
    shop.masters += 1;
    for (const [phase, seconds] of Object.entries(row.timing_seconds || {}))
      shop.seconds[phase] = (shop.seconds[phase] || 0) + seconds;
    shops.set(row.shop_id, shop);
  }
  return (
    <details className="context-note" open>
      <summary>后台任务运行情况 · {rows.length} 项</summary>
      <p>
        每店按时间到期触发，统一调度分配执行名额。仅执行已到期、缺失或失败的子步骤；同店同业务模块不并行。耗时为子步骤累计，上传确认单独计时，诊断记录仅保存到文件。
      </p>
      <details>
        <summary>按店铺汇总耗时 · {shops.size} 家</summary>
        <p>累计各子步骤最近执行耗时，包含并行，不能当作墙钟总耗时；未到期或停用的子步骤不会因此启动。</p>
        {[...shops].map(([id, shop]) => (
          <div key={id}>
            <strong>
              {shop.name} · {shop.masters} 个主任务
            </strong>
            <TaskTiming step={{ timing_seconds: shop.seconds, timing_basis: '子步骤累计' }} />
          </div>
        ))}
      </details>
      <label>
        状态{' '}
        <select
          value={filter}
          onChange={(e) => {
            setFilter(e.target.value);
            setPage(1);
          }}
        >
          <option value="">全部</option>
          {Object.entries(states).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>
      </label>
      <div className="data-table-wrap">
        <SortableTable>
          <thead>
            <tr>
              <th>{t('队列序号 / 预计开始')}</th>
              <th>店铺</th>
              <th>主任务</th>
              <th>状态</th>
              <th>当前子步骤 / 等待原因</th>
              <th>子步骤</th>
              <th>耗时与上传确认</th>
            </tr>
          </thead>
          <tbody>
            {filtered.slice((current - 1) * 20, current * 20).map((r) => (
              <tr key={`${r.shop_id}:${r.master_id}`}>
                <td style={{ verticalAlign: 'top' }}>
                  {r.queue_position ? (
                    <>
                      <b style={{ overflowWrap: 'anywhere' }}>{r.queue_number || `M${r.queue_position}`}</b>
                      <small> / {r.queue_total ?? '—'}</small>
                      <br />
                      <small>{waitText(r.wait_seconds, t)}</small>
                    </>
                  ) : (
                    '—'
                  )}
                </td>
                <td translate="no" style={{ verticalAlign: 'top' }}>
                  {shopLabel(r)}
                </td>
                <td style={{ verticalAlign: 'top' }}>{r.module}</td>
                <td style={{ verticalAlign: 'top' }}>{states[r.phase] || r.phase}</td>
                <td style={{ verticalAlign: 'top' }}>
                  {r.current_step}
                  <small>{r.stage}</small>
                </td>
                <td style={{ verticalAlign: 'top' }}>
                  <details>
                    <summary>
                      查看 {r.query_count ?? r.step_count} 个查询 · {r.step_count} 个内部范围
                    </summary>
                    {r.steps.map((s) => (
                      <div key={s.cache_key}>
                        <strong>{s.name}</strong>
                        <TaskTiming step={s} />
                        <p>
                          {s.frequency} · {s.stage}
                          {s.queue_position ? (
                            <>
                              <br />
                              {t('子步骤')} {s.queue_number || `S${s.queue_position}`} · {waitText(s.wait_seconds, t)}
                              {s.expected_seconds ? ' · ' + t('预计耗时约 {n} 秒', { n: s.expected_seconds }) : ''}
                            </>
                          ) : null}
                          <br />
                          最近成功：{stamp(s.last_success)}
                          {s.range_steps?.map((part) => (
                            <span key={part.cache_key}>
                              <br />
                              {part.name}：{states[part.phase] || part.phase} · {part.frequency}
                              {part.continuation ? ' · 断点已保存' : ''}
                              {part.last_error ? ` · 历史失败：${part.last_error}` : ''}
                            </span>
                          ))}
                          {s.continuation ? ' · 已保留分页断点' : ''}
                          {s.last_error ? (
                            <>
                              <br />
                              历史失败：{s.last_error}
                            </>
                          ) : null}
                        </p>
                      </div>
                    ))}
                  </details>
                </td>
                <td style={{ verticalAlign: 'top' }}>
                  <TaskTiming step={r} />
                </td>
              </tr>
            ))}
          </tbody>
        </SortableTable>
      </div>
      {!filtered.length && <p>当前没有该状态的主任务。</p>}
      <p>
        第 {current} / {pages} 页{' '}
        <button disabled={current === 1} onClick={() => setPage(current - 1)}>
          上一页
        </button>{' '}
        <button disabled={current === pages} onClick={() => setPage(current + 1)}>
          下一页
        </button>
      </p>
    </details>
  );
}
