// Human text for a queued task's estimated wait (seconds until it starts).
const identity = (source, values) =>
  values ? source.replace(/\{(\w+)\}/g, (m, k) => (k in values ? String(values[k]) : m)) : source;

export function waitText(seconds, t = identity) {
  if (seconds == null) return t('预计开始时间暂无法估算');
  if (seconds < 5) return t('即将开始');
  if (seconds < 60) return t('约 {n} 秒后开始', { n: Math.round(seconds) });
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return t('约 {n} 分钟后开始', { n: minutes });
  return t('约 {h} 小时 {m} 分钟后开始', { h: Math.floor(minutes / 60), m: minutes % 60 });
}
