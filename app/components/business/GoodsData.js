'use client';
import ShopOperations from './ShopOperations';
import AdminDialog from '../shared/AdminDialog';
import { NativeButton, Choice } from '../shared/AdminControls';
import { chartSegments } from '../../lib/chart-segments.mjs';
import EmptyData from '../shared/EmptyData';
import PlatformModules from './PlatformModules';
import CollapsibleSection from '../shared/CollapsibleSection';
import UpdatedAgo from '../shared/UpdatedAgo';
import { shopLabel } from '../../lib/shop-identity.mjs';
import SortableTable from '../shared/SortableTable';
import { useEffect, useState } from 'react';
import Thumbnail from '../shared/ProductImage';
import TaobaoTraffic from './TaobaoTraffic';
const num = (v) => (v == null ? '—' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 }));
const money = (v) =>
  v == null ? '—' : '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const percent = (v) => (v == null ? '—' : num(v) + '%');
const columns = [
  ['visitors', '商品访客数', num],
  ['views', '商品浏览量', num],
  ['sales', '成交金额', money],
  ['quantity', '成交件数', num],
  ['orders', '成交订单数', num],
  ['buyers', '成交买家数', num],
  ['conversion', '成交转化率', percent],
  ['favorites', '收藏用户数', num],
  ['order_rate', '下单率', percent],
  ['payment_rate', '成交率', percent],
  ['traffic_loss', '流量损失指数', num],
  ['refund_amount', '退款金额', money],
  ['cart_quantity', '加购件数', num],
];
const scopes = { realtime: '今日实时', yesterday: '昨日', week: '近 7 日', month: '近 30 日' };
function Table({ headers, children, sort }) {
  return (
    <div className="data-table-wrap">
      <SortableTable className="data-table" serverSort={sort}>
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
export default function GoodsData({
  platform,
  sort,
  data,
  page,
  pageSize,
  onPage,
  onPageSize,
  onOpen,
  shopRecords,
  selectedShopId,
  onNavigate,
}) {
  const [detail, setDetail] = useState(null),
    [shopId, setShopId] = useState(''),
    [series, setSeries] = useState('今日累计'),
    [metric, setMetric] = useState('visitors');
  const shops = data?.metrics || [],
    shop = shops.find((s) => String(s.shop_id) === shopId) || shops[0],
    rows = data?.rows || [];
  useEffect(() => {
    setDetail((old) =>
      old ? (data?.rows || []).find((r) => r.shop_id === old.shop_id && r.id === old.id) || null : null,
    );
  }, [data]);
  const seriesOptions = [...new Set((shop?.trend || []).map((r) => r.series))],
    activeSeries = seriesOptions.includes(series) ? series : seriesOptions[0];
  const trend = (shop?.trend || []).filter((r) => r.series === activeSeries),
    max = Math.max(1, ...trend.map((r) => r[metric] || 0)),
    format = columns.find(([key]) => key === metric)?.[2] || num;
  const realtime = data?.scope?.trade_range === 'realtime';
  return (
    <div className="goods-analytics">
      <PlatformModules
        platform={platform}
        specific={[{ id: 'taobao-traffic', platform: 'taobao', content: <TaobaoTraffic shops={shops} /> }]}
        common={
          <>
            <CollapsibleSection className="data-module">
              <h3 className="data-section-title">
                商品概况 <small>{scopes[data?.scope?.trade_range] || '读取中'} · 已读取店铺合计</small>
              </h3>
              <div className="metric-grid">
                {[
                  ['visitors', '商品访客数合计', num],
                  ['views', '商品浏览量', num],
                  ['favorites', realtime ? '昨日收藏用户数合计' : '收藏用户数合计', num],
                  ['visited_goods', '被访问商品数', num],
                  ['sales', '成交金额', money],
                  ['orders', '成交订单数', num],
                ].map(([key, label, f]) => (
                  <div className="metric-card" key={key}>
                    <span>{label}</span>
                    <strong>{f(data?.goods_totals?.[key])}</strong>
                  </div>
                ))}
              </div>
              <details className="context-note">
                <summary>统计说明</summary>
                <p>
                  概况按所选店铺和时间范围统计，不受商品搜索影响。人数跨店相加未去重，单品访客或买家数不可直接相加作为店铺人数。实时模式的概况收藏用户数为昨日口径；近
                  7 日 / 30 日截至昨日。
                </p>
              </details>
              <ShopOperations
                data={data}
                shops={shopRecords}
                shopId={selectedShopId}
                platform={platform}
                onSelectShop={setShopId}
                onNavigate={onNavigate}
                onOpen={onOpen}
              />
            </CollapsibleSection>
            {shop && (
              <details open className="trade-section">
                <summary>商品概况趋势</summary>
                <div className="trade-detail-heading">
                  <label>
                    店铺
                    <Choice aria-label="商品趋势店铺" value={shop.shop_id} onChange={(e) => setShopId(e.target.value)}>
                      {shops.map((s) => (
                        <option key={s.shop_id} value={s.shop_id}>
                          {shopLabel(s)}
                        </option>
                      ))}
                    </Choice>
                  </label>
                  <label>
                    口径
                    <Choice value={activeSeries || ''} onChange={(e) => setSeries(e.target.value)}>
                      {seriesOptions.map((s) => (
                        <option key={s}>{s}</option>
                      ))}
                    </Choice>
                  </label>
                  <label>
                    指标
                    <Choice value={metric} onChange={(e) => setMetric(e.target.value)}>
                      {columns
                        .filter(([k]) =>
                          ['visitors', 'views', 'sales', 'orders', 'buyers', 'conversion', 'favorites'].includes(k),
                        )
                        .map(([key, label]) => (
                          <option key={key} value={key}>
                            {label}
                          </option>
                        ))}
                    </Choice>
                  </label>
                </div>
                {trend.some((r) => r[metric] != null) ? (
                  <>
                    <svg
                      className="trade-chart"
                      viewBox="0 0 900 220"
                      role="img"
                      aria-label="商品概况趋势图，明细在下方表格"
                    >
                      <line x1="60" x2="860" y1="170" y2="170" stroke="#dce4eb" />
                      <text x="4" y="25">
                        {num(max)}
                      </text>
                      {chartSegments(
                        trend,
                        metric,
                        (r, i) => `${60 + (i * 800) / Math.max(1, trend.length - 1)},${170 - (r[metric] / max) * 140}`,
                      ).map((points, i) => (
                        <polyline key={i} fill="none" stroke="#156f70" strokeWidth="3" points={points} />
                      ))}
                      {trend.map((r, i) =>
                        r[metric] == null ? null : (
                          <g key={r.id}>
                            <circle
                              cx={60 + (i * 800) / Math.max(1, trend.length - 1)}
                              cy={170 - ((r[metric] || 0) / max) * 140}
                              r="4"
                              fill="#156f70"
                            >
                              <title>
                                {r.date} {r.hour || ''}：{format(r[metric])}
                              </title>
                            </circle>
                            {i % Math.max(1, Math.ceil(trend.length / 7)) === 0 && (
                              <text x={60 + (i * 800) / Math.max(1, trend.length - 1)} y="205" textAnchor="middle">
                                {r.hour == null ? r.date?.slice(5) : r.hour + ':00'}
                              </text>
                            )}
                          </g>
                        ),
                      )}
                    </svg>
                    <Table
                      headers={[
                        '日期 / 时段',
                        '商品访客数',
                        '浏览量',
                        '成交金额',
                        '成交订单数',
                        '成交买家数',
                        '转化率',
                      ]}
                    >
                      {trend.map((r) => (
                        <tr key={r.id}>
                          <td className="nowrap">
                            {r.date}
                            {r.hour == null ? '' : ` ${r.hour}:00`}
                          </td>
                          {['visitors', 'views', 'sales', 'orders', 'buyers', 'conversion'].map((key) => (
                            <td key={key} data-sort-value={r[key]}>
                              {columns.find(([k]) => k === key)[2](r[key])}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </Table>
                  </>
                ) : (
                  <p className="table-empty">平台暂无趋势数据。</p>
                )}
                <details className="context-note">
                  <summary>统计说明</summary>
                  <p>累计趋势不能按小时重复相加。昨日视图展示平台提供的分时累计趋势。</p>
                </details>
              </details>
            )}
            <div className="table-caption">
              <strong>商品明细 · 匹配 {data?.total ?? '—'} 件</strong>
              <span>支持按指标排序 · {realtime ? '详情可查看昨日对比' : '详情可查看上一周期对比'}</span>
            </div>
            <Table
              sort={{ ...sort, fields: ['shop_name', 'name', ...columns.map(([key]) => key)] }}
              headers={['店铺', '商品', ...columns.map(([, label]) => label), '操作']}
            >
              {rows.map((r) => (
                <tr data-shop-id={r.shop_id} key={`${r.shop_id}:${r.id}`}>
                  <td>{shopLabel(r)}</td>
                  <td className="wide-cell">
                    <div className="goods-with-thumbnail">
                      <Thumbnail url={r.thumbnail_url} name={r.name} />
                      <div title={[r.name, r.goods_id].filter(Boolean).join(' · ')}>
                        <strong>{r.name}</strong>
                        <small>ID：{r.goods_id}</small>
                      </div>
                    </div>
                  </td>
                  {columns.map(([key, , f]) => (
                    <td key={key} className="nowrap">
                      {f(r[key])}
                      {key === 'conversion' && r.conversion_level === '低' && (
                        <small className="error">低于类目参考</small>
                      )}
                    </td>
                  ))}
                  <td>
                    <NativeButton className="table-action" onClick={() => setDetail(r)}>
                      详情
                    </NativeButton>
                    <NativeButton
                      className="table-action"
                      onClick={() => window.dispatchEvent(new CustomEvent('product-overview', { detail: r }))}
                    >
                      商品综合详情
                    </NativeButton>
                  </td>
                </tr>
              ))}
            </Table>
            {!rows.length && <EmptyData loading={!data} sources={data?.sources} />}
            <div className="pagination">
              <span>
                第 {page} / {data?.page_count || 1} 页
              </span>
              <Choice aria-label="每页数量" value={pageSize} onChange={(e) => onPageSize(Number(e.target.value))}>
                {[20, 50, 100].map((size) => (
                  <option key={size} value={size}>
                    {size} 条 / 页
                  </option>
                ))}
              </Choice>
              <NativeButton disabled={page <= 1} onClick={() => onPage(page - 1)}>
                上一页
              </NativeButton>
              <NativeButton disabled={!data || page >= data.page_count} onClick={() => onPage(page + 1)}>
                下一页
              </NativeButton>
            </div>
          </>
        }
      />
      {detail && (
        <AdminDialog label="商品数据详情">
          <section className="admin-dialog-content record-dialog" aria-label="商品数据详情">
            <div className="dialog-head">
              <h2>商品数据详情</h2>
              <NativeButton onClick={() => setDetail(null)}>关闭</NativeButton>
            </div>
            <div className="goods-with-thumbnail">
              <Thumbnail url={detail.thumbnail_url} name={detail.name} />
              <h3>{detail.name}</h3>
            </div>
            <p>
              {shopLabel(detail)} · 商品 ID：{detail.goods_id} · 更新{' '}
              <UpdatedAgo value={data?.sources?.find((s) => s.shop_id === detail.shop_id)?.updated_at} />
            </p>
            <Table headers={['指标', '当前数值', realtime ? '昨日数值' : '上一周期变化']}>
              {columns.map(([key, label, f]) => (
                <tr key={key}>
                  <td>{label}</td>
                  <td>{f(detail[key])}</td>
                  <td>
                    {realtime
                      ? f(detail.previous?.[key])
                      : detail.comparison?.[key]?.value == null
                        ? '—'
                        : num(detail.comparison?.[key].value) + (detail.comparison?.[key].percent ? '%' : '')}
                  </td>
                </tr>
              ))}
            </Table>
            <p>类目平均转化率：{percent(detail.category_conversion)}</p>
            <NativeButton className="primary" onClick={() => onOpen(detail.shop_id, 'goodsdata')}>
              打开该店铺商品数据
            </NativeButton>
          </section>
        </AdminDialog>
      )}
    </div>
  );
}
