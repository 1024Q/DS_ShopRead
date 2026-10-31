'use client';
import { chartSegments } from '../../lib/chart-segments.mjs';
import CollapsibleSection from '../shared/CollapsibleSection';
import { useState } from 'react';
import SortableTable from '../shared/SortableTable';
import { ProductInfo } from '../shared/ProductImage';
import { shopLabel } from '../../lib/shop-identity.mjs';
const num = (v) => (v == null ? '—' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 }));
const columns = [
  ['shop_visitors', '店铺访客'],
  ['item_visitors', '商品访客'],
  ['shop_views', '店铺浏览量'],
  ['item_views', '商品浏览量'],
  ['buyers', '支付买家'],
  ['sales', '支付金额（元）'],
  ['conversion', '支付转化率（%）'],
];
export default function TaobaoTraffic({ shops }) {
  const [selected, setSelected] = useState(''),
    [metric, setMetric] = useState('shop_visitors');
  const available = shops.filter((s) => s.platform === 'taobao' && s.traffic),
    shop = available.find((s) => String(s.shop_id) === selected) || available[0];
  if (!shop) return null;
  const traffic = shop.traffic,
    rows = traffic.trend || [],
    series = [...new Set(rows.map((r) => r.series))],
    max = Math.max(1, ...rows.map((r) => r[metric] || 0));
  return (
    <CollapsibleSection className="trade-section taobao-traffic">
      <div className="trade-detail-heading">
        <h3>淘宝 · 流量看板</h3>
        <label>
          店铺
          <select aria-label="流量看板店铺" value={shop.shop_id} onChange={(e) => setSelected(e.target.value)}>
            {available.map((s) => (
              <option key={s.shop_id} value={s.shop_id}>
                {shopLabel(s)}
              </option>
            ))}
          </select>
        </label>
        <label>
          趋势指标
          <select aria-label="流量看板指标" value={metric} onChange={(e) => setMetric(e.target.value)}>
            {columns.map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <details className="context-note">
        <summary>统计说明</summary>
        <p>淘宝为生意参谋商品排行及流量看板。未提供商品整体去重概况，不累加单品人数；不作为库存或上新记录。</p>
      </details>
      <div className="metric-grid">
        {columns.map(([key, label]) => (
          <article className="metric-card" key={key}>
            <span>{label}</span>
            <strong>{num(traffic.summary?.[key])}</strong>
          </article>
        ))}
      </div>
      <p className="data-note">
        {traffic.note} · 平台时间：{traffic.source_time}
        {shop.stale ? ' · 上次数据' : ''}
      </p>
      <svg className="trade-chart" viewBox="0 0 900 210" role="img" aria-label="淘宝流量趋势，下方可展开数值">
        <line x1="60" y1="165" x2="860" y2="165" stroke="currentColor" opacity=".2" />
        {series.map((name, index) => {
          const points = rows.filter((r) => r.series === name),
            color = index ? '#f59e0b' : '#14b8a6';
          return (
            <g key={name}>
              <text x={60 + index * 180} y="20" fill={color}>
                {name}
              </text>
              {chartSegments(
                points,
                metric,
                (r, i) => `${60 + (i * 800) / Math.max(points.length - 1, 1)},${165 - (r[metric] / max) * 120}`,
              ).map((segment, i) => (
                <polyline key={i} fill="none" stroke={color} strokeWidth="3" points={segment} />
              ))}
              {points.map((r, i) =>
                r[metric] == null ? null : (
                  <circle
                    key={r.id}
                    cx={60 + (i * 800) / Math.max(points.length - 1, 1)}
                    cy={165 - (r[metric] / max) * 120}
                    r="3"
                    fill={color}
                  >
                    <title>
                      {name} {r.date} {r.hour == null ? '' : r.hour + ':00'}：{num(r[metric])}
                    </title>
                  </circle>
                ),
              )}
            </g>
          );
        })}
      </svg>
      <details open>
        <summary>查看分时 / 每日流量明细</summary>
        <div className="data-table-wrap">
          <SortableTable className="data-table">
            <thead>
              <tr>
                <th>口径</th>
                <th>日期 / 时段</th>
                {columns.map(([, label]) => (
                  <th key={label}>{label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>{r.series}</td>
                  <td>
                    {r.date} {r.hour == null ? '' : r.hour + ':00'}
                  </td>
                  {columns.map(([key]) => (
                    <td key={key}>{num(r[key])}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </SortableTable>
        </div>
      </details>
      {traffic.ranking_available ? (
        <>
          <div className="table-caption">
            <strong>流量来源 Top 10 · 无线端</strong>
          </div>
          <div className="data-table-wrap">
            <SortableTable className="data-table">
              <thead>
                <tr>
                  <th>渠道</th>
                  <th>访客数</th>
                  <th>支付买家</th>
                  <th>转化率（%）</th>
                </tr>
              </thead>
              <tbody>
                {traffic.channels.map((r) => (
                  <tr key={r.id}>
                    <td>{r.name}</td>
                    <td data-sort-value={r.visitors}>{num(r.visitors)}</td>
                    <td data-sort-value={r.buyers}>{num(r.buyers)}</td>
                    <td data-sort-value={r.conversion}>{num(r.conversion)}</td>
                  </tr>
                ))}
              </tbody>
            </SortableTable>
          </div>
          <details open>
            <summary>商品流量榜 Top 10 · 无线端</summary>
            <div className="data-table-wrap">
              <SortableTable className="data-table">
                <thead>
                  <tr>
                    <th>商品</th>
                    <th>访客数</th>
                    <th>支付买家</th>
                    <th>转化率（%）</th>
                  </tr>
                </thead>
                <tbody>
                  {traffic.items.map((r) => (
                    <tr key={r.goods_id}>
                      <td>
                        <ProductInfo row={r} />
                      </td>
                      <td data-sort-value={r.visitors}>{num(r.visitors)}</td>
                      <td data-sort-value={r.buyers}>{num(r.buyers)}</td>
                      <td data-sort-value={r.conversion}>{num(r.conversion)}</td>
                    </tr>
                  ))}
                </tbody>
              </SortableTable>
            </div>
          </details>
        </>
      ) : (
        <p className="data-note">
          {traffic.ranking_note || '实时模式展示流量概况与分时趋势；渠道及商品流量榜请切换历史统计范围。'}
        </p>
      )}
      {shop.ranking_error && <p className="data-note">{shop.ranking_error}</p>}
    </CollapsibleSection>
  );
}
