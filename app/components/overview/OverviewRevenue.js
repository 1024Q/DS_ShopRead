'use client';
import { Statistic } from 'antd';
import YesterdayComparison, { ComparisonArrow } from './YesterdayComparison';
const valid = (v) => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
const num = (v) => (valid(v) ? Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '—');
const money = (v) =>
  valid(v) ? '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—';
const updated = (times) =>
  times.length === 1 ? times[0] : times.length > 1 ? `${times[0]} 至 ${times.at(-1)}` : '尚未读取';
export default function OverviewRevenue({ totals, rows, count, times, previous }) {
  return (
    <section className="ov-revenue ov-realtime-revenue" aria-label="平台实时口径统计">
      <div className="ov-hero-top">
        <span>成交金额</span>
        <div className="ov-hero-indicators">
          <ComparisonArrow metric={previous?.fields?.sales} date={previous?.date} />
          <span className="ov-hero-tag">平台实时口径</span>
        </div>
      </div>
      <Statistic className="ov-revenue-value" value={totals.sales ?? 0} formatter={() => money(totals.sales)} />
      <YesterdayComparison metric={previous?.fields?.sales} date={previous?.date} money />
      <span className="ov-coverage">
        {rows.filter((r) => valid(r.sales)).length} / {count} 家已读取
      </span>
      <div className="ov-hero-divider" />
      <div className="ov-hero-bottom">
        <div>
          <span>成交订单</span>
          <strong>
            {num(totals.orders)}
            <small> 笔</small>
          </strong>
          <YesterdayComparison metric={previous?.fields?.orders} date={previous?.date} />
          <ComparisonArrow metric={previous?.fields?.orders} date={previous?.date} />
        </div>
        <div>
          <span>今日评价</span>
          <strong>
            {num(totals.reviews_today)}
            <small> 条</small>
          </strong>
          <YesterdayComparison metric={previous?.fields?.reviews_today} date={previous?.date} />
          <ComparisonArrow metric={previous?.fields?.reviews_today} date={previous?.date} />
        </div>
      </div>
      <p>平台更新：{updated(times)} · 各店铺统计时点可能不同</p>
    </section>
  );
}
