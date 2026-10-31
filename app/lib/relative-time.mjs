export function relativeTime(value, now = Date.now()) {
  const stamp = typeof value === 'number' ? value * 1000 : Date.parse(value);
  if (!Number.isFinite(stamp)) return '尚未更新';
  const seconds = Math.max(0, Math.floor((now - stamp) / 1000));
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分钟前`;
  return `${Math.floor(seconds / 86400)} 天 ${Math.floor((seconds % 86400) / 3600)} 小时前`;
}
