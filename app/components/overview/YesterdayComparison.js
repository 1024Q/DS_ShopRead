'use client';
export function ComparisonArrow({ metric, date }) {
  if (!metric?.comparable) return null;
  const direction = metric.direction;
  const label =
    metric.change == null
      ? direction === 'up'
        ? '新增'
        : direction === 'flat'
          ? '持平'
          : '—'
      : `${Math.abs(metric.change).toLocaleString('zh-CN', { maximumFractionDigits: 1 })}%`;
  return (
    <span
      className={`comparison-arrow is-${direction}`}
      title={`${date} · ${metric.basis || '上新记录'}；${metric.comparable} 家店铺同范围对比，非同时段同比`}
      aria-label={`较昨日${direction === 'up' ? '上升' : direction === 'down' ? '下降' : '持平'} ${label}`}
    >
      <svg viewBox="0 0 20 20" aria-hidden="true">
        <path
          d={direction === 'up' ? 'M4 14 14 4M5 4h9v9' : direction === 'down' ? 'M4 6l10 10M5 16h9V7' : 'M3 10h14'}
        />
      </svg>
      {label}
    </span>
  );
}
export default function YesterdayComparison({ metric, money = false, date }) {
  const value = metric?.value;
  return (
    <span
      className="yesterday-comparison"
      title={`${date || '昨日'} · ${metric?.basis || '暂无昨日记录'}；已读取 ${metric?.known || 0} / ${metric?.total || 0} 家`}
    >
      <span>{metric?.label || '昨日'}</span>
      <b>
        {value == null
          ? '—'
          : `${money ? '¥' : ''}${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: money ? 2 : 0, maximumFractionDigits: 2 })}`}
      </b>
    </span>
  );
}
