export const sourceIssue = (s) =>
  s.state !== 'stopped' &&
  !!(
    s.error ||
    s.waiting_login ||
    s.failed_scopes?.length ||
    s.failed_scope_count ||
    ['error', 'blocked'].includes(s.data_status)
  );
export const sourceActive = (s) => !!(s.syncing || ['queued', 'running'].includes(s.data_status));
export function retryUrl(s) {
  if (s.cache_key)
    return `/api/system/retry-scope?${new URLSearchParams({ shop_id: String(s.shop_id), cache_key: s.cache_key })}`;
  const p = new URLSearchParams({ shop_id: String(s.shop_id), force: 'true' });
  if (s.platform) p.set('platform', s.platform);
  for (const k of ['date_from', 'date_to', 'status', 'reply', 'period', 'trade_range'])
    if (s.scope?.[k]) p.set(k, s.scope[k]);
  if (s.scope?._exact_dates) p.set('exact_dates', 'true');
  if (s.scope?.stars?.length) p.set('stars', s.scope.stars.join(','));
  return s.scope?._attention
    ? `/api/attention/sync?${new URLSearchParams({ shop_id: String(s.shop_id), kind: s.kind })}`
    : `/api/data/${s.kind}/sync?${p}`;
}
export function retryCandidates(sources) {
  return [
    ...new Map(
      sources
        .filter((s) => !s.waiting_login && s.state !== 'stopped')
        .flatMap((s) =>
          s.failed_scopes?.length
            ? s.failed_scopes.map((f) => ({ ...s, ...f }))
            : !sourceActive(s) && sourceIssue(s)
              ? [s]
              : [],
        )
        .map((s) => [retryUrl(s), s]),
    ).values(),
  ];
}
export async function retrySource(source, request = fetch) {
  const r = await request(retryUrl(source), { method: 'POST', signal: AbortSignal.timeout(20000) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Error(typeof data.detail === 'string' ? data.detail : '重采提交失败');
  if (Array.isArray(data.tasks) && !data.tasks.length) throw Error('当前没有可采集任务');
  if (Array.isArray(data.tasks) && data.tasks.some((t) => t.syncing === false))
    throw Error('尚未进入采集队列，请检查登录或远程控制状态');
  return data;
}
export async function retrySourceGroup(source, request = fetch) {
  const response = await request(
    '/api/system/sources?' +
      new URLSearchParams({
        shop_id: String(source.shop_id),
        kind: source.kind,
        cadence: source.queue || '',
        details: 'true',
      }),
  );
  if (!response.ok) throw Error('无法读取失败范围，请稍后重试');
  const data = await response.json(),
    latest = data.rows?.find(
      (s) => s.shop_id === source.shop_id && s.kind === source.kind && (!source.queue || s.queue === source.queue),
    );
  if (!latest) throw Error('数据源已变化，请刷新后重试');
  if (latest.failed_scopes?.length) {
    const result = await retrySources([latest], null, request);
    if (result.failed.length)
      throw Error(`已提交 ${result.submitted} 项；${result.failed.map((f) => f.message).join('；')}`);
    return result;
  }
  if (sourceIssue(latest)) return retrySource(latest, request);
  return { skipped: true };
}
// Submit one command at a time; the server controls actual collection concurrency.
export async function retrySources(sources, onProgress, request = fetch) {
  const candidates = retryCandidates(sources),
    result = { total: candidates.length, submitted: 0, failed: [] };
  for (const source of candidates) {
    try {
      await retrySource(source, request);
      result.submitted++;
    } catch (error) {
      result.failed.push({
        source,
        message: error.name === 'TimeoutError' ? '请求超时，请核对队列状态' : error.message,
      });
    }
    onProgress?.({ ...result, completed: result.submitted + result.failed.length });
  }
  return result;
}
