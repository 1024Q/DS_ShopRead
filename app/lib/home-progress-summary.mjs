export const overdueLabels = {
  queued: '到期排队',
  waking: '唤醒店铺',
  sleeping: '休眠待轮转',
  running: '正在读取',
  saving: '正在入库',
  retry: '异常待恢复',
  waiting: '等待派发',
};

export function overdueBreakdown(rows = []) {
  const counts = {};
  for (const row of rows)
    for (const item of row.modules || []) {
      if (!item.included || item.group === 'daily' || !item.overdue) continue;
      const status = item.status;
      const key =
        item.overdue_reason ||
        (status === 'running'
          ? 'running'
          : status === 'sleeping'
            ? 'sleeping'
            : ['error', 'retry_wait'].includes(status)
              ? 'retry'
              : ['queued', 'deferred'].includes(status)
                ? 'queued'
                : 'waiting');
      counts[key] = (counts[key] || 0) + 1;
    }
  return Object.entries(overdueLabels)
    .filter(([key]) => counts[key])
    .map(([key, label]) => ({ key, label, count: counts[key] }));
}
