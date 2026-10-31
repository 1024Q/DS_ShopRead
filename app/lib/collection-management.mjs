export const runLabels = { running: '正在执行', waiting: '等待执行', issue: '异常待处理' };
export function stepState(step) {
  if (step.phase === 'running' || step.slot_granted) return 'running';
  const recovered = step.last_success && step.last_success >= (step.last_attempt || Infinity);
  return !recovered && (step.blocked || step.waiting_login || step.failures > 0) ? 'issue' : 'waiting';
}
export function leafSteps(masters) {
  return masters.flatMap((m) => (m.steps || []).flatMap((s) => (s.range_steps?.length ? s.range_steps : [s])));
}
export function uploadSummary(steps) {
  const rows = steps.filter((s) => s.upload_timing);
  const pending = rows.filter((s) => s.upload_timing.state !== 'confirmed');
  const failed = pending.filter((s) => s.upload_timing.state === 'failed');
  const dates = pending.map((s) => s.last_timing?.finished_at).filter(Number.isFinite);
  return {
    state: failed.length ? 'failed' : pending.length ? 'pending' : rows.length ? 'confirmed' : 'unpublished',
    pending: pending.length,
    confirmed: rows.length - pending.length,
    oldest: dates.length ? Math.min(...dates) : null,
    error: [...new Set(failed.map((s) => s.upload_timing.reason).filter(Boolean))].join('；'),
  };
}
export function shopRows(shops, masters = []) {
  const groups = new Map();
  for (const m of masters) {
    const list = groups.get(m.shop_id) || [];
    list.push(m);
    groups.set(m.shop_id, list);
  }
  return shops.map((shop) => {
    const tasks = groups.get(shop.id) || [],
      steps = leafSteps(tasks);
    const running = steps.filter((s) => stepState(s) === 'running');
    const issues = steps.filter((s) => stepState(s) === 'issue');
    const stopped = shop.state === 'stopped';
    const state = stopped
      ? 'waiting'
      : running.length
        ? 'running'
        : issues.length || ['needs_verification', 'logged_out', 'error'].includes(shop.state)
          ? 'issue'
          : 'waiting';
    const current = running[0] || issues[0] || steps.find((s) => s.stage && s.stage !== '等待下次执行');
    const next = steps.map((s) => s.next_run_at || s.retry_at).filter((v) => Number.isFinite(v) && v > 0);
    return {
      ...shop,
      key: shop.id,
      tasks,
      steps,
      runState: state,
      current,
      waitingReason: stopped
        ? '店铺已停止，不会自动启动'
        : running.length
          ? ''
          : current?.stage || (shop.state === 'sleeping' ? '休眠，等待下次到期' : '等待下次到期'),
      nextAt: next.length ? Math.min(...next) : null,
      nodeName: shop.machine_name || shop.node_name || tasks[0]?.machine_name || '本机',
      upload: uploadSummary(steps),
      runningCount: running.length,
      issueCount: issues.length,
    };
  });
}
export function orderedIds(rows, field, direction = 'ascend') {
  return [...rows]
    .sort((a, b) => {
      const av = a[field] ?? '',
        bv = b[field] ?? '';
      const result =
        typeof av === 'number' && typeof bv === 'number'
          ? av - bv
          : String(av).localeCompare(String(bv), 'zh-CN', { numeric: true });
      return (
        (direction === 'descend' ? -result : result) ||
        String(a.key).localeCompare(String(b.key), undefined, { numeric: true })
      );
    })
    .map((r) => r.key);
}
export function csvCell(value) {
  let text = String(value ?? '');
  if (/^[=+\-@\t\r]/.test(text)) text = "'" + text;
  return '"' + text.replaceAll('"', '""') + '"';
}

// Original file rows use created/last_seen/occurrences. Only the displayed
// error rows are combined; file contents and pagination cursors stay intact.
export function executionLogRows(rows = []) {
  const values = [],
    errors = new Map();
  for (const raw of rows) {
    const row = {
      ...raw,
      first_seen: raw.first_seen ?? raw.created ?? raw.last_seen,
      count: raw.count ?? raw.occurrences ?? 1,
    };
    if (!['error', 'warning'].includes(row.level)) {
      values.push(row);
      continue;
    }
    const key = JSON.stringify([row.shop_id, row.module, row.event, row.cache_key, row.message]);
    const previous = errors.get(key);
    if (previous) {
      previous.first_seen = Math.min(previous.first_seen, row.first_seen);
      previous.last_seen = Math.max(previous.last_seen, row.last_seen);
      previous.count += row.count;
    } else {
      errors.set(key, row);
      values.push(row);
    }
  }
  return values;
}
