'use client';
import { ActionButton } from '../shared/AdminControls';
import CollapsibleSection from '../shared/CollapsibleSection';
import ModuleQueueBoard from './ModuleQueueBoard';
import QueueDiagnostics from './QueueDiagnostics';
import ConcurrencySettings from './ConcurrencySettings';
import ClearQueueButton from './ClearQueueButton';
import MasterTasks from './MasterTasks';
export default function CollectionQueues({ status, onFilter, cadence, defaultExpanded = true }) {
  const config = status?.collection,
    queues = status?.queues;
  if (!config) return null;
  const available = !!queues;
  return (
    <CollapsibleSection className="collection-queues" aria-label="采集队列" defaultExpanded={defaultExpanded}>
      <div className="queue-heading">
        <div>
          <h3>
            采集队列 <small>当前选择范围</small>
          </h3>
          <p>
            每店按后台任务分组 · 子步骤按各自频率执行 · {status.diagnostics?.merged_demands || 0} 项复用合并 ·{' '}
            {status.summary?.deferred || 0} 项待调度 · {status.summary?.queued || 0} 项排队中
          </p>
        </div>
        <div className="queue-actions">
          <ClearQueueButton />
          <span>
            {status.scope === 'all' ? '已读取机器总上限' : '本机上限'}{' '}
            <b>{status.configured_max_concurrent ?? config.max_concurrent ?? queues?.max_concurrent ?? '—'}</b> 个 ·
            单店铺 <b>{config.shop_concurrent ?? '各机器不同'}</b> 个
          </span>
        </div>
      </div>
      <ConcurrencySettings />
      {!available && <p className="notice">当前机器尚未提供队列状态，请先更新该机器的 Python 服务。</p>}
      <div className="queue-cards">
        {(config.frequency_groups || [])
          .map((g) => [g.id, g.name, g.time ? '每日 ' + g.time : '每 ' + g.seconds + ' 秒'])
          .map(([id, title, frequency]) => {
            const q = queues?.[id];
            return (
              <ActionButton
                key={id}
                className={`queue-card ${cadence === id ? 'selected' : ''}`}
                aria-pressed={cadence === id}
                onClick={() => onFilter(cadence === id ? '' : id)}
              >
                <div>
                  <strong>{title}</strong>
                  <small>{frequency}</small>
                </div>
                <div className="queue-counts">
                  <span>
                    <b>{q?.running ?? '—'}</b> 运行
                  </span>
                  <span>
                    <b>{q?.queued ?? '—'}</b> 排队
                  </span>
                  <small>共享节点与店铺并发名额</small>
                </div>
              </ActionButton>
            );
          })}
      </div>
      {status.machine?.effective_limit != null && (
        <p className="queue-note">
          本机有效并发 <b>{status.machine.effective_limit}</b> / 配置上限 {status.machine.configured_limit} · CPU{' '}
          {status.machine.cpu_percent ?? '—'}% · 内存 {status.machine.memory_percent ?? '—'}% · {status.machine.reason}
          。降低并发时等待现有任务自然结束。
        </p>
      )}
      <ModuleQueueBoard config={config} />
      <MasterTasks rows={status.master_tasks} />
      <QueueDiagnostics status={status} />
      <details className="context-note">
        <summary>统计说明</summary>
        <p>
          点击队列可筛选下方数据源；队列数量按主任务统计，内部子步骤不重复计数；下方数据源仍分别展示各业务数据。采集间隔是到期检查频率，完成时间取决于排队和平台响应。同店铺使用独立采集页并行。
        </p>
        <p>
          保留节点总并发与单店并发；同店同业务模块互斥，不同业务模块按现有并发配置执行。后台任务只组织子步骤，频率分组不分配独立并发额度。完成任务后立即补位；降低并发时等待正在运行的任务完成。
        </p>
      </details>
    </CollapsibleSection>
  );
}
