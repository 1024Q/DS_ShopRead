'use client';
import { NativeButton, TextInput, Choice } from '../shared/AdminControls';
import { chartSegments } from '../../lib/chart-segments.mjs';
import PlatformModules from './PlatformModules';
import CollapsibleSection from '../shared/CollapsibleSection';
import UpdatedAgo from '../shared/UpdatedAgo';
import { shopLabel } from '../../lib/shop-identity.mjs';
import SortableTable from '../shared/SortableTable';
import OnDemandDetails from './OnDemandDetails';
import { useState } from 'react';

const num = (v) => (v == null ? '—' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 }));
const money = (v) =>
  v == null ? '—' : '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const percent = (v) => (v == null ? '—' : num(v) + '%');
const fields = [
  ['sales', '成交金额', money],
  ['orders', '成交订单数', num],
  ['buyers', '成交买家数', num],
  ['conversion', '成交转化率', percent],
  ['average', '客单价', money],
  ['returning_rate', '成交老买家占比', percent],
  ['followers', '关注用户数', num],
  ['refund_amount', '退款金额', money],
  ['refund_orders', '退款单数', num],
  ['visitor_value', '访客价值', money],
];
const ranges = { realtime: '今日实时', yesterday: '昨日', week: '近 7 日', month: '近 30 日' };
function Chart({ rows, metric, format }) {
  const available = rows.filter((r) => r[metric] != null);
  if (!available.length) return <p className="table-empty">平台暂无此指标趋势。</p>;
  const max = Math.max(...available.map((r) => r[metric]), 1),
    x = (i) => 60 + (i * 800) / Math.max(rows.length - 1, 1),
    y = (v) => 170 - ((v || 0) / max) * 140;
  return (
    <svg className="trade-chart" viewBox="0 0 900 220" role="img" aria-label="交易趋势图，完整数值可在下方趋势明细查看">
      {[0, 0.5, 1].map((n) => (
        <g key={n}>
          <line x1="60" x2="865" y1={y(n * max)} y2={y(n * max)} stroke="#e1e7ed" />
          <text x="3" y={y(n * max) - 6}>
            {num(n * max)}
          </text>
        </g>
      ))}
      {chartSegments(rows, metric, (r, i) => `${x(i)},${y(r[metric])}`).map((points, i) => (
        <polyline key={i} fill="none" stroke="#156f70" strokeWidth="3" points={points} />
      ))}
      {rows.map((r, i) =>
        r[metric] == null ? null : (
          <g key={r.id}>
            <circle cx={x(i)} cy={y(r[metric])} r="4" fill="#156f70">
              <title>
                {r.date} {r.hour == null ? '' : r.hour + ':00'}：{format(r[metric])}
              </title>
            </circle>
            {(i % Math.max(1, Math.ceil(rows.length / 7)) === 0 || i === rows.length - 1) && (
              <text x={x(i)} y="202" textAnchor="middle">
                {r.hour == null ? r.date.slice(5) : r.hour + ':00'}
              </text>
            )}
          </g>
        ),
      )}
    </svg>
  );
}
function Table({ headers, children }) {
  return (
    <div className="data-table-wrap">
      <SortableTable className="data-table">
        <thead>
          <tr>
            {headers.map((h) => (
              <th key={h}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </SortableTable>
    </div>
  );
}

export default function TransactionData({ data, platform, onCollectDetails, loading = false }) {
  const [selected, setSelected] = useState(''),
    [series, setSeries] = useState('今日累计'),
    [metric, setMetric] = useState('sales'),
    [region, setRegion] = useState('');
  const shops = data?.metrics || [],
    shop = shops.find((s) => String(s.shop_id) === selected) || shops[0];
  const seriesOptions = [...new Set((shop?.trend || []).map((r) => r.series))];
  const activeSeries = seriesOptions.includes(series) ? series : seriesOptions[0];
  const trend = (shop?.trend || []).filter((r) => r.series === activeSeries);
  const format = fields.find(([k]) => k === metric)?.[2] || num;
  const realtime = data?.scope?.trade_range === 'realtime';
  const hasTaobao = shops.some((s) => s.platform === 'taobao'),
    allPdd = shops.every((s) => s.platform !== 'taobao');
  const priorOnly = (key) =>
    shop?.platform !== 'taobao' && ['followers', 'refund_amount', 'refund_orders', 'visitor_value'].includes(key);
  const regions = (shop?.regions || [])
    .filter((r) => (r.name || '').includes(region))
    .sort((a, b) => (b.sales ?? -1) - (a.sales ?? -1));
  const ordersModule = shop?.platform === 'pdd' && (
    <CollapsibleSection className="data-module">
      <h3 className="data-section-title">
        拼多多 · 订单数据 <small>{shop.orders_info?.time}</small>
      </h3>
      <div className="metric-grid trade-order-metrics">
        {[
          ['pending_orders', '下单待付款订单数', num],
          ['pending_amount', '下单待付款金额', money],
          ['settlement_amount', '待到账金额', money],
        ].map(([key, label, f]) => (
          <div className="metric-card" key={key}>
            <span>{label}</span>
            <strong>{f(shop.orders_info?.[key])}</strong>
          </div>
        ))}
      </div>
      <p className="data-note">
        催付数据 · 截至 {shop.lead?.through || '尚未读取日期'} 的近 30 日：引导下单人数 {num(shop.lead?.buyers)}
        ，引导成交金额 {money(shop.lead?.sales)}。
      </p>
    </CollapsibleSection>
  );
  return (
    <div className="transaction-data">
      <OnDemandDetails sources={data?.sources} label="附属统计" onCollect={onCollectDetails} busy={loading} />
      <PlatformModules
        platform={platform}
        specific={[{ id: 'pdd-orders', platform: 'pdd', content: shop?.platform === 'pdd' ? ordersModule : null }]}
        common={
          <>
            <CollapsibleSection className="data-module">
              <h3 className="data-section-title">
                交易概况 <small>{ranges[data?.scope?.trade_range] || '读取中'} · 已读取店铺合计</small>
              </h3>
              <div className="metric-grid trade-totals">
                {fields
                  .filter(([k]) => ['sales', 'orders', 'buyers', 'refund_amount', 'refund_orders'].includes(k))
                  .map(([key, label, format]) => (
                    <div className="metric-card" key={key}>
                      <span>
                        {realtime && allPdd && key.startsWith('refund_') ? '昨日' : ''}
                        {label}
                        {key === 'buyers'
                          ? '合计'
                          : realtime && hasTaobao && !allPdd && key.startsWith('refund_')
                            ? '（按平台统计日）'
                            : ''}
                      </span>
                      <strong>{format(data?.trade_totals?.[key])}</strong>
                    </div>
                  ))}
              </div>
              <details className="context-note">
                <summary>统计说明</summary>
                <p>
                  买家数按店铺相加，未跨店去重；转化率和客单价按店铺展示。拼多多实时模式的关注、退款和访客价值为昨日数据；淘宝退款为当前所选周期的完结退款，订单数为支付子订单数。近
                  7 日 / 30 日截至昨日。
                </p>
              </details>
              {hasTaobao && (
                <details className="context-note">
                  <summary>统计说明</summary>
                  <p>
                    淘宝多日自定义人数按生意参谋未去重口径展示。平台统计口径不同，退款请查看店铺明细；不支持的指标显示
                    —。
                  </p>
                </details>
              )}
              <Table headers={['店铺', '成交金额', '成交订单数', '成交买家数', '转化率', '客单价', '平台统计时间']}>
                {shops.map((s) => (
                  <tr data-shop-id={s.shop_id} key={s.shop_id}>
                    <td>
                      <NativeButton className="table-action" onClick={() => setSelected(String(s.shop_id))}>
                        {shopLabel(s)}
                      </NativeButton>
                      {s.stale && (
                        <small className="error">
                          上次数据 · <UpdatedAgo value={s.updated_at} />
                        </small>
                      )}
                    </td>
                    {['sales', 'orders', 'buyers', 'conversion', 'average'].map((key) => (
                      <td key={key} data-sort-value={s.summary[key]}>
                        {fields.find(([k]) => k === key)[2](s.summary[key])}
                      </td>
                    ))}
                    <td className="date-cell">{s.source_time || '未提供'}</td>
                  </tr>
                ))}
              </Table>
            </CollapsibleSection>
            {!shop ? (
              <div className="table-empty">
                {data?.sources?.some((s) => s.syncing) ? '正在读取交易数据…' : '暂无已读取数据，请查看店铺同步状态。'}
              </div>
            ) : (
              <>
                <CollapsibleSection className="data-module">
                  <div className="trade-detail-heading">
                    <h3>店铺交易明细</h3>
                    <label>
                      查看店铺
                      <Choice
                        aria-label="查看店铺明细"
                        value={shop.shop_id}
                        onChange={(e) => setSelected(e.target.value)}
                      >
                        {shops.map((s) => (
                          <option key={s.shop_id} value={s.shop_id}>
                            {shopLabel(s)}
                          </option>
                        ))}
                      </Choice>
                    </label>
                    <span>{shop.source_time}</span>
                  </div>
                  <div className="metric-grid trade-detail-metrics">
                    {fields.map(([key, label, format]) => {
                      const comparison = shop.comparisons?.[key];
                      return (
                        <div className="metric-card" key={key}>
                          <span>
                            {shop.realtime && priorOnly(key) ? '昨日' : ''}
                            {label}
                          </span>
                          <strong>{format(shop.summary?.[key])}</strong>
                          {comparison?.value != null && (
                            <small>
                              {shop.realtime && !priorOnly(key)
                                ? '昨日：'
                                : shop.realtime
                                  ? '较前一日：'
                                  : '较上一周期：'}
                              {shop.realtime && !priorOnly(key)
                                ? format(comparison.value)
                                : `${comparison.rising ? '↑' : '↓'} ${num(comparison.value)}${comparison.percent ? '%' : ''}`}
                            </small>
                          )}
                        </div>
                      );
                    })}
                  </div>
                  {shop.scope_note && <p className="data-note">{shop.scope_note}</p>}
                </CollapsibleSection>
                <CollapsibleSection className="data-module">
                  <div className="trade-detail-heading">
                    <h3>交易趋势</h3>
                    <label>
                      统计口径
                      <Choice
                        aria-label="趋势口径"
                        value={activeSeries || ''}
                        onChange={(e) => setSeries(e.target.value)}
                      >
                        {seriesOptions.map((s) => (
                          <option key={s}>{s}</option>
                        ))}
                      </Choice>
                    </label>
                    <label>
                      趋势指标
                      <Choice aria-label="趋势指标" value={metric} onChange={(e) => setMetric(e.target.value)}>
                        {fields.map(([key, label]) => (
                          <option key={key} value={key}>
                            {label}
                          </option>
                        ))}
                      </Choice>
                    </label>
                  </div>
                  <Chart rows={trend} metric={metric} format={format} />
                  <details className="context-note">
                    <summary>统计说明</summary>
                    <p>
                      累计口径不可将各小时相加；分时买家数未跨小时去重。趋势日期以平台返回为准，淘宝昨日视图展示昨日数据；拼多多昨日视图包含平台提供的近
                      7 日趋势。
                    </p>
                  </details>
                  <details open className="trade-section">
                    <summary>趋势明细（{trend.length} 条）</summary>
                    <Table headers={['日期 / 时段', ...fields.map(([, label]) => label)]}>
                      {trend.map((r) => (
                        <tr key={r.id}>
                          <td className="nowrap">
                            {r.date}
                            {r.hour == null ? '' : ` ${r.hour}:00`}
                          </td>
                          {fields.map(([key, , f]) => (
                            <td key={key} data-sort-value={r[key]}>
                              {f(r[key])}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </Table>
                  </details>
                </CollapsibleSection>
                <details className="trade-section" open>
                  <summary>年度经营情况</summary>
                  {shop.annual?.length ? (
                    <Table
                      headers={[
                        '月份',
                        `${shop.annual[0]?.previous_year || '上'}年成交额`,
                        `${shop.annual[0]?.year || '本'}年成交额`,
                        '目标成交额',
                        '每月完成度',
                      ]}
                    >
                      {shop.annual.map((r) => (
                        <tr key={r.month}>
                          <td>{r.month}月</td>
                          <td data-sort-value={r.previous_sales}>{money(r.previous_sales)}</td>
                          <td data-sort-value={r.sales}>{money(r.sales)}</td>
                          <td data-sort-value={r.target}>{money(r.target)}</td>
                          <td data-sort-value={r.completion}>{percent(r.completion)}</td>
                        </tr>
                      ))}
                    </Table>
                  ) : (
                    <p className="table-empty">
                      {shop.details_on_demand && !shop.details_updated_at
                        ? '尚未采集年度经营数据，可点击采集附属统计。'
                        : '平台未提供年度经营数据。'}
                    </p>
                  )}
                </details>
                <details className="trade-section" open>
                  <summary>地区交易数据 · {shop.range === 'realtime' ? '昨日' : ranges[shop.range]}</summary>
                  <div className="trade-detail-heading">
                    <label>
                      省份筛选
                      <TextInput
                        aria-label="省份筛选"
                        placeholder="输入省份名称"
                        value={region}
                        onChange={(e) => setRegion(e.target.value)}
                      />
                    </label>
                    <span>共 {regions.length} 个地区 · 按成交金额排序</span>
                  </div>
                  <Table
                    headers={['省份', '统计日期', '成交金额', '成交订单数', '成交买家数', '成交订单单价', '成交客单价']}
                  >
                    {regions.map((r) => (
                      <tr key={r.id}>
                        <td>{r.name}</td>
                        <td>{r.date}</td>
                        <td data-sort-value={r.sales}>{money(r.sales)}</td>
                        <td data-sort-value={r.orders}>{num(r.orders)}</td>
                        <td data-sort-value={r.buyers}>{num(r.buyers)}</td>
                        <td data-sort-value={r.order_average}>{money(r.order_average)}</td>
                        <td data-sort-value={r.buyer_average}>{money(r.buyer_average)}</td>
                      </tr>
                    ))}
                  </Table>
                  {!regions.length && (
                    <p className="table-empty">
                      {shop.details_on_demand && !shop.details_updated_at
                        ? '尚未采集地区数据，可点击采集附属统计。'
                        : '当前条件下暂无地区数据。'}
                    </p>
                  )}
                </details>
              </>
            )}
          </>
        }
      />
    </div>
  );
}
