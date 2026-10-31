'use client';
import { ActionButton } from '../shared/AdminControls';
import { Alert, Segmented, Slider, Statistic } from 'antd';
import { HomeMetricCard } from '../../providers/HomeTheme';
import HomeCollectionControl from './HomeCollectionControl';
import { reportSpend } from '../../lib/overview-ads.mjs';
import { overviewTarget } from '../../lib/overview-navigation.mjs';
import { overviewCounts, countBasisDetail, countBasisBrief } from '../../lib/overview-counts.mjs';
import useOverviewCounts from '../../hooks/useOverviewCounts';
import ListCompletionPanel from '../collection/ListCompletionPanel';
import { chartSegments } from '../../lib/chart-segments.mjs';
import MultiSelect from '../shared/MultiSelect';
import usePersistentSelection from '../../hooks/usePersistentSelection';
import YesterdayComparison, { ComparisonArrow } from './YesterdayComparison';
import { inScope } from '../../lib/filter-scope.mjs';
import AttentionPanel from './AttentionPanel';
import { AttentionNoticeEntry } from './AttentionNotice';
import ContributionRing from './ContributionRing';
import UpdatedAgo from '../shared/UpdatedAgo';
import DataCoverage from '../collection/DataCoverage';
import CollapsibleSection from '../shared/CollapsibleSection';
import PlatformFilter from '../shared/PlatformFilter';
import { databaseReader } from '../../lib/database-reader';
import { shopLabel } from '../../lib/shop-identity.mjs';
import { useEffect, useId, useRef, useState } from 'react';
import { aggregateTrend } from '../../lib/performance.mjs';
import { canReadScope, canReadShop, platforms } from '../../lib/platforms.mjs';
import NewProducts from './NewProducts';
import OverviewPerformance from './OverviewPerformance';
import { recordPerformance } from '../../lib/ui-performance.mjs';
import DeferredSection from '../shared/DeferredSection';
import OverviewRevenue from './OverviewRevenue';
import { refundSummary } from '../../lib/refund-summary.mjs';
import useReportData from '../../hooks/useReportData';
import { useWorkspace, useGlobalRefresh, useDatabaseUpdates } from '../../providers/WorkspaceContext';

const valid = (v) => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
const num = (v) => (valid(v) ? Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '—');
const money = (v) =>
  valid(v) ? '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—';
const colors = ['#198B7C', '#D0A34B', '#6394AC', '#7C859F', '#9BA769', '#BD8272'];
const compact = (v) => (v >= 10000 ? num(v / 10000) + '万' : v >= 1000 ? num(v / 1000) + '千' : num(v));
function Coverage({ rows, field, total }) {
  return (
    <span className="ov-coverage">
      {rows.filter((r) => valid(r[field])).length} / {total} 家已读取
    </span>
  );
}
// Compact: one short status and the update time. Full basis, list count and freshness on hover.
function CountBasis({ value }) {
  const brief = countBasisBrief(value);
  return (
    <small className="ov-count-meta" title={countBasisDetail(value)}>
      {brief && <span className="ov-count-issue">{brief}</span>}
      {brief && value?.updated_at && ' · '}
      {value?.updated_at && <UpdatedAgo value={value.updated_at} />}
    </small>
  );
}

function Trend({ platform, shops, shopId, auto, autoReady, refreshVersion, onNavigate, suppliedData }) {
  const readOnly = suppliedData !== undefined;
  const [selection, setSelection] = usePersistentSelection('overview-trend-shops'),
    [metric, setMetric] = useState('sales'),
    [point, setPoint] = useState(null);
  const { businessDay } = useWorkspace();
  const [expanded, setExpanded] = useState(true);
  const selected = String(shopId || selection || '');
  const readable = canReadScope(shops, selected, 'transactions');
  const report = useReportData(
    'transactions',
    { shop_id: selected, platform, trade_range: 'realtime', dashboard: true },
    !readOnly && readable && expanded,
  );
  const data = readOnly ? suppliedData : report.data,
    error = readOnly ? suppliedData?.error : report.error;
  const gradient = useId().replace(/:/g, '');
  const visibleData = readable
    ? readOnly
      ? {
          ...data,
          metrics: data?.metrics?.filter((m) => inScope(selected, m.shop_id)),
          sources: data?.sources?.filter((source) => inScope(selected, source.shop_id)),
        }
      : data
    : null;
  const sources = visibleData?.sources || [],
    source = {
      syncing: sources.some((s) => s.syncing),
      error: sources.some((s) => s.error) ? '部分店铺读取失败' : null,
      stale: sources.some((s) => s.stale),
      waiting_login: sources.some((s) => s.waiting_login),
    };
  const today = businessDay,
    yesterday = new Date(new Date(today + 'T00:00:00+08:00').getTime() - 1).toLocaleDateString('sv-SE', {
      timeZone: 'Asia/Shanghai',
    });
  const combined = aggregateTrend(visibleData?.metrics, metric, today, yesterday),
    trend = combined.rows;
  const series = ['今日累计', '昨日累计'].map((name) =>
    trend.filter((r) => r.series === name && valid(r[metric])).sort((a, b) => Number(a.hour) - Number(b.hour)),
  );
  const max = Math.max(1, ...trend.map((r) => (valid(r[metric]) ? Number(r[metric]) : 0)));
  const x = (h) => 80 + (Number(h) / 23) * 640,
    y = (v) => 218 - (Number(v) / max) * 178;
  const format = metric === 'sales' ? money : num;
  const chosen = point ?? Math.max(0, ...series[0].map((r) => Number(r.hour)));
  const points = series.map((rows) => rows.find((r) => Number(r.hour) === chosen));
  return (
    <CollapsibleSection className="ov-card ov-trend" onExpandedChange={setExpanded}>
      <div className="ov-card-head">
        <div>
          <h3>成交走势</h3>
        </div>
        <ActionButton
          className="ov-text-button"
          onClick={() => onNavigate('transactions', { shop_id: selected, trade_range: 'realtime' })}
        >
          交易数据 ↗
        </ActionButton>
      </div>
      <div className="ov-chart-controls">
        <Segmented
          aria-label="走势指标"
          value={metric}
          onChange={(key) => {
            setMetric(key);
            setPoint(null);
          }}
          options={[
            ['sales', '成交额'],
            ['orders', '订单数'],
            ['buyers', '买家数'],
          ].map(([value, label]) => ({ value, label }))}
        />
        <label className="ov-trend-shop">
          趋势店铺
          <MultiSelect
            label="趋势店铺"
            value={selected}
            onChange={(value) => setSelection(value)}
            options={shops.map((s) => ({ value: s.id, label: shopLabel(s) }))}
            placeholder="全部店铺"
            disabled={!!shopId}
          />
        </label>
      </div>
      <div className="ov-chart-legend">
        {data?._preview_cached_at && <small>上次结果 · 正在核对数据库</small>}
        <span>
          <i />
          今日累计
        </span>
        <span>
          <i />
          昨日累计
        </span>
        <small>
          {source?.syncing
            ? '按小时累计'
            : source?.error || error
              ? '读取失败，保留已读取趋势'
              : source?.stale
                ? '上次读取的趋势'
                : '按小时累计'}
        </small>
      </div>
      {series.some((s) => s.length) ? (
        <>
          <div className="ov-chart-readout" aria-live="polite">
            <span>{String(chosen).padStart(2, '0')}:00</span>
            {points.map((p, i) => (
              <span key={i}>
                {i ? '昨日' : '今日'} <strong>{format(p?.[metric])}</strong>
              </span>
            ))}
          </div>
          <svg
            className="ov-line-chart"
            viewBox="0 0 750 255"
            role="img"
            aria-label="当前所选店铺的今日与昨日累计成交走势；下方可按小时查看准确值"
            onMouseLeave={() => setPoint(null)}
          >
            <defs>
              <linearGradient id={gradient} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#198B7C" stopOpacity=".2" />
                <stop offset="100%" stopColor="#198B7C" stopOpacity="0" />
              </linearGradient>
            </defs>
            {[0, 0.25, 0.5, 0.75, 1].map((t) => (
              <g key={t}>
                <line x1="80" x2="720" y1={y(max * t)} y2={y(max * t)} stroke="#E5ECE9" strokeDasharray="3 5" />
                <text x="68" y={y(max * t) + 5} textAnchor="end">
                  {compact(max * t)}
                </text>
              </g>
            ))}
            {[0, 4, 8, 12, 16, 20, 23].map((h) => (
              <text key={h} x={x(h)} y="245" textAnchor="middle">
                {String(h).padStart(2, '0')}:00
              </text>
            ))}
            {series.map((rows, i) => {
              const segments = chartSegments(rows, metric, (r) => `${x(r.hour)},${y(Number(r[metric]))}`);
              const coords = segments[0] || '';
              return (
                <g key={i}>
                  {!i && segments.length === 1 && rows.length > 1 && (
                    <polygon
                      points={`${x(rows[0].hour)},218 ${coords} ${x(rows.at(-1).hour)},218`}
                      fill={`url(#${gradient})`}
                    />
                  )}
                  {segments.map((segment, j) => (
                    <polyline
                      key={j}
                      points={segment}
                      fill="none"
                      stroke={i ? '#C1A365' : '#198B7C'}
                      strokeWidth="3"
                      strokeDasharray={i ? '6 6' : undefined}
                      strokeLinejoin="round"
                    />
                  ))}
                  {rows.map((r) => (
                    <circle
                      key={r.id || r.hour}
                      cx={x(r.hour)}
                      cy={y(Number(r[metric]))}
                      r={Number(r.hour) === chosen ? 5 : 2.5}
                      fill={i ? '#C1A365' : '#198B7C'}
                    >
                      <title>
                        {r.date} {r.hour}:00 {format(r[metric])} · {r.coverage} 家店铺
                      </title>
                    </circle>
                  ))}
                </g>
              );
            })}
            <line x1={x(chosen)} x2={x(chosen)} y1="30" y2="218" stroke="#9AB8AE" strokeDasharray="3 4" />
            {Array.from({ length: 24 }, (_, h) => (
              <rect
                key={h}
                x={x(h) - 14}
                y="25"
                width="28"
                height="195"
                fill="transparent"
                onMouseEnter={() => setPoint(h)}
              />
            ))}
          </svg>
          <label className="ov-hour">
            查看时点
            <Slider
              aria-label="查看趋势小时"
              min={0}
              max={23}
              value={chosen}
              onChange={setPoint}
              tooltip={{ formatter: (h) => `${String(h).padStart(2, '0')}:00` }}
            />
            <span>{String(chosen).padStart(2, '0')}:00</span>
          </label>
          <p className="ov-footnote">
            今日：{series[0][0]?.date || '未读取'} · 昨日：{series[1][0]?.date || '未读取'}
            。按各小时已返回数据汇总，悬停查看覆盖店铺数；更新较慢的店铺不阻塞其他店铺，缺失不补零。
          </p>
        </>
      ) : (
        <div className="ov-chart-empty">
          <span className="ov-empty-lines" aria-hidden="true" />
          <strong>
            {!shops.length
              ? '添加店铺后查看走势'
              : !readable
                ? '业务数据待接入'
                : source?.syncing
                  ? '正在读取交易趋势'
                  : source?.waiting_login
                    ? '店铺登录后可查看走势'
                    : '暂无可用趋势'}
          </strong>
          <p>{error || source?.error || '暂无可比数据'}</p>
        </div>
      )}
      {(error || source?.error) && (
        <p className="ov-footnote ov-warning" role="alert">
          {error || source.error}
        </p>
      )}
    </CollapsibleSection>
  );
}

export default function OverviewDashboard({
  data,
  platform,
  shops,
  shopId,
  onShopChange,
  auto,
  autoReady,
  onAutoChange,
  loading,
  syncing,
  error,
  onRefresh,
  onNavigate,
  onOpen,
  tools,
  publicSnapshot,
}) {
  const readOnly = publicSnapshot !== undefined;
  const started = useRef(performance.now()),
    measured = useRef(false);
  useEffect(() => {
    if (data && !measured.current) {
      recordPerformance('首页首批数据显示', performance.now() - started.current);
      measured.current = true;
    }
  }, [data]);
  const root = useRef(null),
    [refreshVersion, setRefreshVersion] = useState({ version: 0, force: false });
  useGlobalRefresh((mode) => setRefreshVersion((v) => ({ version: v.version + 1, force: mode === 'collect' })));
  const tradeReport = useReportData(
    'transactions',
    { shop_id: shopId, platform, trade_range: 'realtime', dashboard: true },
    !readOnly && canReadScope(shops, shopId, 'transactions'),
  );
  const trades = readOnly ? publicSnapshot?.trades : tradeReport.data,
    tradeError = readOnly ? publicSnapshot?.trades?.error : tradeReport.error;
  const adReport = useReportData(
    'ad_reports',
    { shop_id: shopId, platform, trade_range: 'realtime', dashboard: true },
    !readOnly && canReadScope(shops, shopId, 'ad_reports'),
  );
  const ads = readOnly ? publicSnapshot?.ads : adReport.data,
    adError = readOnly ? publicSnapshot?.ads?.error : adReport.error;
  const { businessDay: today } = useWorkspace();
  const countScope = { shop_id: shopId, platform };
  const queried_shippingAlerts = useOverviewCounts(
    'shipping_due',
    countScope,
    !readOnly && canReadScope(shops, shopId, 'orders'),
  );
  const shippingAlerts = readOnly ? publicSnapshot?.counts?.shipping_due : queried_shippingAlerts;
  const queried_aftersalesAlerts = useOverviewCounts(
    'aftersales_due',
    countScope,
    !readOnly && canReadScope(shops, shopId, 'aftersales'),
  );
  const aftersalesAlerts = readOnly ? publicSnapshot?.counts?.aftersales_due : queried_aftersalesAlerts;
  const queried_shippingPending = useOverviewCounts(
    'pending_shipping',
    countScope,
    !readOnly && canReadScope(shops, shopId, 'orders'),
  );
  const shippingPending = readOnly ? publicSnapshot?.counts?.pending_shipping : queried_shippingPending;
  const queried_aftersalesPending = useOverviewCounts(
    'pending_aftersales',
    countScope,
    !readOnly && canReadScope(shops, shopId, 'aftersales'),
  );
  const aftersalesPending = readOnly ? publicSnapshot?.counts?.pending_aftersales : queried_aftersalesPending;
  const queried_ticketsPending = useOverviewCounts(
    'pending_tickets',
    countScope,
    !readOnly && canReadScope(shops, shopId, 'tickets'),
  );
  const ticketsPending = readOnly ? publicSnapshot?.counts?.pending_tickets : queried_ticketsPending;

  const refundReport = useReportData(
    'orders',
    { shop_id: shopId, platform: 'pdd', status: '退款成功', date_from: today, date_to: today, dashboard: true },
    !readOnly && canReadScope(shops, shopId, 'orders'),
  );
  const orderRefunds = readOnly ? publicSnapshot?.refunds : refundReport.data;
  const refunds = refundSummary(trades, shops, shopId, today, orderRefunds);
  const todayRefund = refunds.groups.find((g) => g.date === today);
  const scope = shops.filter((s) => inScope(shopId, s.id));
  const adMetrics = ads?.metrics || [],
    adTotals = ads?.ad_totals || {};
  const adSpendFor = (id) => reportSpend(adMetrics, scope.find((s) => s.id === id) || { id });
  const spendValues = scope.map((s) => adSpendFor(s.id)).filter(valid);
  const originalRows = scope.map((s) => ({
    ...refunds.rows.find((r) => r.shop_id === s.id),
    ...data?.sources?.find((r) => r.shop_id === s.id),
    ...data?.metrics?.find((r) => r.shop_id === s.id),
    shop_id: s.id,
    shop_name: s.name || `店铺 ${s.id}`,
    shop_label: shopLabel(s),
    platform: s.platform || 'pdd',
    pending_adapter: !canReadShop(s),
    ad_spend: adSpendFor(s.id),
  }));
  const countProjection = overviewCounts(originalRows, {
    shipping_due: shippingAlerts,
    aftersales_due: aftersalesAlerts,
    pending_shipping: shippingPending,
    pending_aftersales: aftersalesPending,
    pending_tickets: ticketsPending,
  });
  const rows = countProjection.rows;
  const sources = data?.sources || [],
    totals = {
      ...data?.totals,
      ...countProjection.totals,
      ad_spend: spendValues.length ? spendValues.reduce((n, v) => n + Number(v), 0) : null,
    };
  const count = scope.length,
    pending = scope.filter((s) => !canReadShop(s)).length;
  const warnings = sources.filter((s) => s.error || s.waiting_login || s.stale || !s.updated_at);
  const times = [...new Set(sources.map((s) => s.source_time).filter(Boolean))].sort();
  const lastUpdated = sources
    .map((s) => s.updated_at)
    .filter(Boolean)
    .sort()
    .at(-1);
  const lastReceived = sources
    .map((s) => s.center_received_at)
    .filter(Boolean)
    .sort()
    .at(-1);
  const go = (kind, filters = {}) => onNavigate?.(kind, { shop_id: shopId, platform, ...filters });
  const openCount = (metric, sid = shopId) => {
    const target = overviewTarget(metric, { shop_id: sid, platform });
    if (target) go(target.kind, target.filters);
  };
  return (
    <section className="overview-dashboard" ref={root}>
      <div className="ov-heading">
        <div>
          <h2>
            实时经营概览
            <span>
              最近采集完成 <UpdatedAgo value={lastUpdated} /> · 中心接收{' '}
              {lastReceived ? <UpdatedAgo value={lastReceived} /> : '尚无接收时间记录'}
            </span>
          </h2>
        </div>
        <div className="ov-heading-actions">
          <div className="ov-controls">
            {readOnly ? (
              <span>全部店铺 · {shops.length} 家</span>
            ) : (
              <>
                <PlatformFilter />
                <label className="ov-shop-select">
                  店铺范围
                  <MultiSelect
                    label="概览店铺"
                    value={shopId}
                    onChange={(value) => onShopChange(value)}
                    options={shops.map((s) => ({ value: s.id, label: shopLabel(s) }))}
                    placeholder="全部店铺"
                  />
                </label>
              </>
            )}
          </div>
          {!readOnly && <HomeCollectionControl compact />}
        </div>
      </div>
      <style jsx>{`
        .ov-heading-actions {
          display: flex;
          justify-content: flex-end;
          align-items: center;
          gap: 14px;
          flex-wrap: wrap;
          margin-left: auto;
          max-width: 100%;
        }
      `}</style>
      {!readOnly && <AttentionNoticeEntry onNavigate={onNavigate} />}
      {error && <Alert type="warning" showIcon title="部分数据暂不可用" description={error} />}
      <div className="ov-top-grid">
        <OverviewRevenue totals={totals} rows={rows} count={count} times={times} previous={data?.previous} />
        <div className="ov-kpi-grid">
          {[
            ['orders', '今日订单', '笔', 'transactions'],
            ['visitors', '店铺访客', '人次', 'goodsdata'],
            ['views', '商品浏览', '次', 'goodsdata'],
            ['ad_spend', '推广花费', '元', 'ad_reports'],
            ['pending_payment', '待付款', '笔', null],
          ].map(([key, label, unit, kind]) => {
            return (
              <HomeMetricCard
                className="ov-kpi"
                key={key}
                onClick={
                  kind
                    ? () =>
                        go(
                          kind,
                          key === 'orders'
                            ? { trade_range: 'realtime', date_from: '', date_to: '', exact_dates: false, keyword: '' }
                            : {},
                        )
                    : undefined
                }
              >
                <div>
                  <span>{label}</span>
                  {key !== 'ad_spend' && (
                    <ComparisonArrow metric={data?.previous?.fields?.[key]} date={data?.previous?.date} />
                  )}
                </div>
                <Statistic
                  value={totals[key] ?? 0}
                  formatter={() => (key === 'ad_spend' ? money(totals[key]) : num(totals[key]))}
                />{' '}
                {key !== 'ad_spend' ? (
                  <YesterdayComparison metric={data?.previous?.fields?.[key]} date={data?.previous?.date} />
                ) : (
                  <>
                    <div className="ov-ad-details">
                      {[
                        ['roi', '推广 ROI'],
                        ['net_roi', '净 ROI'],
                        ['ad_revenue', '推广交易额'],
                        ['ad_spend', '成交花费'],
                      ].map(([field, title]) => (
                        <span key={field}>
                          {title}
                          <b>{field.includes('roi') ? num(adTotals[field]) : money(adTotals[field])}</b>
                        </span>
                      ))}
                    </div>
                    <small
                      className="ov-ad-source"
                      title="拼多多为今日推广报表总花费；淘宝为阿里妈妈实时报表花费，不含平台排除场景。ROI 按同一报表的推广交易额 ÷ 成交花费计算。"
                    >
                      今日推广报表 ↗{adError ? ' · 暂留上次数据' : ''}
                    </small>
                  </>
                )}
                <div className="ov-kpi-bottom">
                  <Coverage rows={rows} field={key} total={count} />
                  <small>{unit}</small>
                </div>
              </HomeMetricCard>
            );
          })}
          {[
            ['orders', '今日退款订单', '笔'],
            ['amount', '今日退款金额', '元'],
          ].map(([key, label, unit]) => (
            <HomeMetricCard
              className="ov-kpi"
              key={'refund-' + key}
              title="拼多多：今日下单且退款成功，按售后单实际退款金额；淘宝沿用平台当期退款口径"
            >
              <div>
                <span>{label}</span>
              </div>
              <Statistic
                value={todayRefund?.[key] ?? 0}
                formatter={() => (key === 'amount' ? money(todayRefund?.amount) : num(todayRefund?.orders))}
              />
              <div className="ov-kpi-bottom">
                <span>
                  {todayRefund?.[key === 'amount' ? 'amountCoverage' : 'orderCoverage'] || 0} /{' '}
                  {todayRefund?.count || 0} 家已读取
                </span>
                <small>{unit}</small>
              </div>
            </HomeMetricCard>
          ))}
        </div>
        <CollapsibleSection className="ov-attention ov-attention-compact">
          <div className="ov-card-head">
            <div>
              <h3>优先关注</h3>
            </div>
            {!readOnly && (
              <ListCompletionPanel shopIds={scope.filter((s) => (s.platform || 'pdd') === 'pdd').map((s) => s.id)} />
            )}
          </div>
          <div className="ov-priority-top">
            <div className="ov-priority-actions">
              <AttentionPanel
                suppliedData={readOnly ? publicSnapshot?.attention || {} : undefined}
                compact
                platform={platform}
                shopId={shopId}
                onNavigate={() => go('todos', { kind: '', priority: '', keyword: '' })}
              />
              <div className="ov-urgent-grid">
                {[
                  ['shipping_due', '即将逾期发货', 'packages'],
                  ['aftersales_due', '售后过期预警', 'aftersales'],
                ].map(([k, label, kind]) => (
                  <ActionButton
                    key={k}
                    onClick={() => openCount(k)}
                    title={countBasisDetail(countProjection.metrics[k])}
                    className={`ov-count-card ${valid(totals[k]) && Number(totals[k]) > 0 ? 'has-risk' : ''}`}
                  >
                    <span className="ov-count-head">
                      <span className="ov-count-label">{label}</span>
                      <i aria-hidden="true">↗</i>
                    </span>
                    <strong className="ov-count-value">{num(totals[k])}</strong>
                    <CountBasis value={countProjection.metrics[k]} />
                  </ActionButton>
                ))}
              </div>
            </div>
            <ContributionRing rows={rows} total={count} />
          </div>
          <div className="ov-task-list">
            {[
              ['pending_shipping', '待发货', 'packages'],
              [
                'pending_aftersales',
                countProjection.metrics.pending_aftersales.basis === 'list' ? '待商家处理售后' : '退款 / 售后',
                'aftersales',
              ],
              ['pending_tickets', '待处理工单', 'tickets'],
            ].map(([k, label, kind]) => (
              <ActionButton
                className="ov-task-row ov-count-card"
                key={k}
                onClick={() => openCount(k)}
                title={countBasisDetail(countProjection.metrics[k])}
              >
                <span className="ov-count-head">
                  <span className="ov-count-label">{label}</span>
                  <i aria-hidden="true">↗</i>
                </span>
                <strong className="ov-count-value">{num(totals[k])}</strong>
                <CountBasis value={countProjection.metrics[k]} />
              </ActionButton>
            ))}
          </div>
          <details className="ov-attention-note">
            <summary>口径说明</summary>
            待办主数字保留平台首页口径，与已存列表数量不直接等同；预警按列表统计，卡片与店铺行共用计数结果。鼠标悬停卡片可查看范围、列表数量与更新时间。
          </details>
          <Trend
            platform={platform}
            shops={shops}
            shopId={shopId}
            auto={auto}
            autoReady={autoReady}
            refreshVersion={refreshVersion}
            onNavigate={go}
            suppliedData={readOnly ? publicSnapshot?.trades || {} : undefined}
          />
        </CollapsibleSection>
      </div>
      {readOnly ? (
        <OverviewPerformance
          ads={ads}
          trades={trades}
          tradeError={tradeError}
          platform={platform}
          shops={shops}
          shopId={shopId}
          onNavigate={onNavigate}
        />
      ) : (
        <DeferredSection label="各店铺今日与昨日走势">
          <OverviewPerformance
            ads={ads}
            trades={trades}
            tradeError={tradeError}
            platform={platform}
            shops={shops}
            shopId={shopId}
            onNavigate={onNavigate}
          />
        </DeferredSection>
      )}
      {readOnly ? (
        <NewProducts
          platform={platform}
          shops={shops}
          shopId={shopId}
          onNavigate={onNavigate}
          suppliedData={publicSnapshot?.new_products || {}}
        />
      ) : (
        <DeferredSection label="上新统计">
          <NewProducts platform={platform} shops={shops} shopId={shopId} onNavigate={onNavigate} />
        </DeferredSection>
      )}
    </section>
  );
}
