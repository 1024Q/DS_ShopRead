'use client';
import { useId, useState } from 'react';
import { shopLabel } from '../../lib/shop-identity.mjs';
const money = (n) => '¥' + Number(n).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export default function ContributionRing({ rows, total }) {
  const [active, setActive] = useState(null),
    tooltipId = useId();
  const known = rows.filter(
    (r) => r.sales !== null && r.sales !== undefined && r.sales !== '' && Number.isFinite(Number(r.sales)),
  );
  const positive = known.filter((r) => Number(r.sales) > 0).sort((a, b) => Number(b.sales) - Number(a.sales));
  const sum = positive.reduce((n, r) => n + Number(r.sales), 0),
    circumference = 2 * Math.PI * 66;
  const selected = positive.find((r) => r.shop_id === active);
  let offset = 0;
  return (
    <div className="ov-contribution-ring" aria-label="店铺成交贡献">
      <svg viewBox="0 0 180 180" aria-label="店铺成交金额占比，悬停或聚焦色块查看店铺数据">
        <circle cx="90" cy="90" r="66" fill="none" stroke="var(--shell-border)" strokeWidth="22" />
        {positive.map((r, i) => {
          const share = Number(r.sales) / sum,
            start = offset;
          offset += share;
          return (
            <circle
              key={r.shop_id}
              cx="90"
              cy="90"
              r="66"
              fill="none"
              stroke={`hsl(${(i * 137.508 + 155) % 360} 58% 49%)`}
              strokeWidth={active === r.shop_id ? 28 : 22}
              strokeDasharray={`${share * circumference} ${circumference}`}
              strokeDashoffset={-start * circumference}
              transform="rotate(-90 90 90)"
              tabIndex="0"
              role="img"
              aria-label={`${shopLabel(r)}，成交金额 ${money(r.sales)}，占比 ${(share * 100).toFixed(1)}%`}
              aria-describedby={active === r.shop_id ? tooltipId : undefined}
              onMouseEnter={() => setActive(r.shop_id)}
              onMouseLeave={() => setActive(null)}
              onFocus={() => setActive(r.shop_id)}
              onBlur={() => setActive(null)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') setActive(null);
              }}
            >
              <title>
                {shopLabel(r)} · {money(r.sales)} · {(share * 100).toFixed(1)}%
              </title>
            </circle>
          );
        })}
      </svg>
      <div className="ov-ring-center">
        <span>店铺贡献</span>
        <strong>{selected ? ((Number(selected.sales) / sum) * 100).toFixed(1) + '%' : sum ? known.length : '—'}</strong>
        <small>{selected ? '成交占比' : sum ? `/ ${total} 家已读取` : '暂无成交数据'}</small>
      </div>
      {selected && (
        <div className="ov-ring-tooltip" role="tooltip" id={tooltipId}>
          <strong>{shopLabel(selected)}</strong>
          <span>
            成交金额 <b>{money(selected.sales)}</b>
          </span>
          <span>
            成交占比 <b>{((Number(selected.sales) / sum) * 100).toFixed(2)}%</b>
          </span>
          {selected.orders != null && (
            <span>
              成交订单 <b>{Number(selected.orders).toLocaleString('zh-CN')} 笔</b>
            </span>
          )}
        </div>
      )}
    </div>
  );
}
