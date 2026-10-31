export function freshness(source, now = Date.now()) {
  const raw = source.updated_at;
  const stamp = typeof raw === 'number' ? raw * 1000 : Date.parse(raw);
  if (!Number.isFinite(stamp)) return { label: '尚未入库', tone: 'missing' };
  const age = Math.max(0, Math.floor((now - stamp) / 60000));
  const time =
    age === 0
      ? '刚刚更新'
      : age < 60
        ? `${age} 分钟前`
        : age < 1440
          ? `${Math.floor(age / 60)} 小时前`
          : `${Math.floor(age / 1440)} 天前`;
  return {
    label: `${time}${source.stale ? ' · 待更新' : ''}${source.complete === false ? ' · 部分数据' : ''}`,
    tone: source.stale ? 'stale' : source.complete === false ? 'partial' : 'fresh',
  };
}
