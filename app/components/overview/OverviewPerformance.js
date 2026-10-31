'use client';
import { ActionButton } from '../shared/AdminControls';
import { chartSegments } from '../../lib/chart-segments.mjs';
import { inScope } from '../../lib/filter-scope.mjs';
import CollapsibleSection from '../shared/CollapsibleSection';
import { shopLabel } from '../../lib/shop-identity.mjs';
import { useEffect, useId, useState } from 'react';
import { compareTrend } from '../../lib/performance.mjs';
import { canReadScope, canReadShop, platforms } from '../../lib/platforms.mjs';
const value = (v, money = false) =>
  v == null
    ? '—'
    : (money ? '¥' : '') +
      Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2, minimumFractionDigits: money ? 2 : 0 });
// Use layout coordinates so entering a card mid-transition cannot shift its anchor.
const anchorCard = (e) => {
  const card = e.currentTarget,
    grid = card.parentElement;
  card.style.transformOrigin = `${card.offsetLeft < 12 ? 'left' : grid.clientWidth - card.offsetLeft - card.offsetWidth < 12 ? 'right' : 'center'} ${card.offsetTop < 12 ? 'top' : grid.clientHeight - card.offsetTop - card.offsetHeight < 12 ? 'bottom' : 'center'}`;
};
function Lines({ comparison, name }) {
  const gradient = useId().replace(/:/g, '');
  const max = Math.max(1, ...comparison.series.flat().map((p) => p.value)),
    x = (h) => 8 + (h / 23) * 284,
    y = (v) => 70 - (v / max) * 60;
  return (
    <svg className="shop-comparison-chart" viewBox="0 0 300 90" role="img" aria-label={`${name}今日与昨日累计走势`}>
      <defs>
        <linearGradient id={gradient} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#16a6ff" stopOpacity=".35" />
          <stop offset="100%" stopColor="#16a6ff" stopOpacity=".02" />
        </linearGradient>
      </defs>
      {[0.25, 0.5, 0.75, 1].map((v) => (
        <line
          key={v}
          x1="8"
          x2="292"
          y1={y(max * v)}
          y2={y(max * v)}
          stroke="currentColor"
          strokeDasharray="3 5"
          opacity=".12"
        />
      ))}
      {comparison.series[0].length > 1 &&
        chartSegments(comparison.series[0], 'value', (p) => String(p.value)).length === 1 && (
          <polygon
            points={`${x(comparison.series[0][0].hour)},70 ${comparison.series[0].map((p) => `${x(p.hour)},${y(p.value)}`).join(' ')} ${x(comparison.series[0].at(-1).hour)},70`}
            fill={`url(#${gradient})`}
          />
        )}
      <line x1="8" x2="292" y1="70" y2="70" stroke="currentColor" opacity=".12" />
      {comparison.series.map((rows, i) => (
        <g key={i}>
          {chartSegments(rows, 'value', (p) => `${x(p.hour)},${y(p.value)}`).map((segment, j) => (
            <polyline
              key={j}
              points={segment}
              fill="none"
              stroke={i ? '#ae79ef' : '#149bf4'}
              strokeWidth="3"
              strokeDasharray={i ? '5 4' : undefined}
            />
          ))}
          {rows.map((p) => (
            <circle key={p.hour} cx={x(p.hour)} cy={y(p.value)} r="2" fill={i ? '#ae79ef' : '#149bf4'}>
              <title>
                {i ? '昨日' : '今日'} {p.hour} 时：{value(p.value)}
              </title>
            </circle>
          ))}
        </g>
      ))}
      <text x="8" y="87">
        00:00
      </text>
      <text x="150" y="87" textAnchor="middle">
        12:00
      </text>
      <text x="292" y="87" textAnchor="end">
        23:00
      </text>
    </svg>
  );
}
export default function OverviewPerformance({ shops, shopId, onNavigate, platform, trades, tradeError, ads }) {
  const [metric, setMetric] = useState('sales'),
    [motion, setMotion] = useState(true);
  useEffect(() => {
    try {
      setMotion(localStorage.getItem('pdd-manager:trend-motion') !== 'false');
    } catch {}
  }, []);
  const toggleMotion = () => {
    setMotion(!motion);
    try {
      localStorage.setItem('pdd-manager:trend-motion', String(!motion));
    } catch {}
  };
  const rows = shops.filter((s) => inScope(shopId, s.id));
  const date = new Date(Date.now() + 8 * 3600000),
    today = date.toISOString().slice(0, 10),
    yesterday = new Date(date.getTime() - 86400000).toISOString().slice(0, 10);
  return (
    <div className="overview-performance">
      <CollapsibleSection className="ov-card shop-comparisons" data-motion={motion ? 'on' : 'off'}>
        <div className="ov-card-head">
          <div>
            <h3>
              各店铺今日与昨日走势 <small>{rows.length} 家</small>
            </h3>
          </div>
          <div className="comparison-controls">
            <ActionButton
              className="comparison-motion"
              aria-pressed={motion}
              aria-label="走势动画"
              onClick={toggleMotion}
            >
              动画：{motion ? '开启' : '关闭'}
            </ActionButton>
            <div className="ov-segments" aria-label="多店铺走势指标">
              {[
                ['sales', '成交额'],
                ['orders', '订单数'],
                ['buyers', '买家数'],
              ].map(([key, label]) => (
                <ActionButton key={key} aria-pressed={key === metric} onClick={() => setMetric(key)}>
                  {label}
                </ActionButton>
              ))}
            </div>
          </div>
        </div>
        {(ads?._preview_cached_at || trades?._preview_cached_at) && (
          <p className="data-note">上次推广 / 交易结果 · 正在核对数据库</p>
        )}
        <div className="ov-chart-legend performance-legend">
          <span>
            <i />
            今日累计
          </span>
          <span>
            <i />
            昨日累计
          </span>
          <small>按最近共同小时对比</small>
        </div>
        {tradeError && <p className="error">{tradeError}</p>}
        <div className="shop-comparison-grid">
          {rows.map((shop) => {
            const data = trades?.metrics?.find((m) => m.shop_id === shop.id),
              source = trades?.sources?.find((s) => s.shop_id === shop.id),
              ad = ads?.metrics?.find((m) => m.shop_id === shop.id),
              comparison = compareTrend(data?.trend, metric, today, yesterday);
            return (
              <article
                tabIndex={0}
                onPointerEnter={anchorCard}
                onFocus={anchorCard}
                key={shop.id}
                className={`shop-comparison ${comparison.change > 0 ? 'is-rising' : comparison.change < 0 ? 'is-falling' : 'is-neutral'}`}
              >
                <div className="shop-comparison-head">
                  <div>
                    <strong title={shopLabel(shop)}>{shopLabel(shop)}</strong>
                    <small className="comparison-operator">运营 · {shop.operator || '未分配'}</small>
                  </div>
                  <ActionButton
                    className="ov-text-button"
                    aria-label={`查看${shopLabel(shop)}交易数据`}
                    onClick={() => onNavigate('transactions', { shop_id: String(shop.id), trade_range: 'realtime' })}
                  >
                    ↗
                  </ActionButton>
                </div>
                <div className="comparison-metric-content" key={metric} data-metric={metric}>
                  <div className="shop-comparison-values">
                    <div>
                      <span>今日同期</span>
                      <strong>{value(comparison.today, metric === 'sales')}</strong>
                    </div>
                    <div>
                      <span>昨日同期</span>
                      <strong>{value(comparison.yesterday, metric === 'sales')}</strong>
                    </div>
                    <span
                      className={`trend-change ${comparison.change > 0 ? 'rising' : comparison.change < 0 ? 'falling' : ''}`}
                    >
                      {comparison.change != null
                        ? `${comparison.change > 0 ? '↑ +' : comparison.change < 0 ? '↓ ' : ''}${value(comparison.change)}%`
                        : comparison.yesterday === 0
                          ? '昨日同期为 0'
                          : '待对比'}
                    </span>
                  </div>
                  {comparison.series.some((s) => s.length) ? (
                    <Lines comparison={comparison} name={shopLabel(shop)} />
                  ) : (
                    <div className="shop-comparison-empty">
                      {!canReadShop(shop)
                        ? '业务数据待接入'
                        : source?.syncing
                          ? '走势同步中…'
                          : source?.waiting_login
                            ? '等待店铺登录'
                            : '暂无今日可比数据'}
                    </div>
                  )}
                  <div className="comparison-gap">
                    <span>同期差额</span>
                    <strong>
                      {comparison.today == null || comparison.yesterday == null
                        ? '—'
                        : `${comparison.today - comparison.yesterday > 0 ? '+' : ''}${value(comparison.today - comparison.yesterday, metric === 'sales')}`}
                    </strong>
                  </div>
                </div>
                <div className="shop-comparison-footer">
                  <span>
                    {comparison.hour != null
                      ? `共同小时 ${String(comparison.hour).padStart(2, '0')} 时`
                      : '等待共同小时数据'}
                    {source?.stale ? ' · 上次记录' : ''}
                  </span>
                  <span>
                    推广 ROI <b>{value(ad?.roi)}</b>
                  </span>
                </div>
              </article>
            );
          })}
        </div>
        <p className="ov-footnote">
          增减幅按双方都有数据的最近小时计算；今日未到的时点不补零。昨日曲线展示平台已返回时点，缺失数据不视为零。
        </p>
      </CollapsibleSection>
    </div>
  );
}
