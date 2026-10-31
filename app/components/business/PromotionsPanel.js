'use client';
import AdminDialog from '../shared/AdminDialog';
import { NativeButton, TextInput, Choice } from '../shared/AdminControls';
import { preferenceKey } from '../../lib/preference-scope.mjs';
import EmptyData from '../shared/EmptyData';
import useQueryLocation from '../../hooks/useQueryLocation';
import MultiSelect from '../shared/MultiSelect';
import { inScope, validSelection, scopePlatform } from '../../lib/filter-scope.mjs';
import StatusBadge from '../shared/StatusBadge';
import DataCoverage from '../collection/DataCoverage';
import OnDemandDetails from './OnDemandDetails';
import CollapsibleSection from '../shared/CollapsibleSection';
import PlatformFilter from '../shared/PlatformFilter';
import { shopLabel } from '../../lib/shop-identity.mjs';
import SortableTable from '../shared/SortableTable';
import { useEffect, useMemo, useRef, useState } from 'react';
import { sortedRows } from '../../lib/table-sort.mjs';
import useReportData from '../../hooks/useReportData';
import { ProductInfo } from '../shared/ProductImage';
import { SavedFilters, TablePreferences, ExportButton } from '../shell/WorkspaceTools';
const money = (v) =>
  v == null ? '—' : '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const num = (v) => (v == null ? '—' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 }));
export const adFields = [
  ['ad_spend', '成交花费', money],
  ['ad_revenue', '推广交易额', money],
  ['roi', '实际投产比', num],
  ['total_spend', '总花费', money],
  ['net_revenue', '净交易额', money],
  ['net_roi', '净实际投产比', num],
  ['settlement_revenue', '结算交易额', money],
  ['settlement_roi', '结算投产比', num],
  ['ad_orders', '成交笔数', num],
  ['net_orders', '净成交笔数', num],
  ['impressions', '曝光量', num],
  ['clicks', '点击量', num],
  ['ctr', '点击率', (v) => (v == null ? '—' : num(v) + '%')],
  ['cvr', '点击成交率', (v) => (v == null ? '—' : num(v) + '%')],
];
export default function PromotionsPanel({ kind, shops, onOpen, initialFilters, platform = '' }) {
  const [pageSize, setPageSize] = useState(20),
    [detail, setDetail] = useState(null);
  const [dailySort, setDailySort] = useState({}),
    root = useRef(null),
    [filters, setFilters] = useState({
      shop_id: '',
      trade_range: 'realtime',
      keyword: '',
      status: '',
      ...initialFilters,
    }),
    [draft, setDraft] = useState(''),
    [ready, setReady] = useState(false),
    [page, setPage] = useState(1),
    [dailyPage, setDailyPage] = useState(1);
  useEffect(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(preferenceKey('pdd-manager:filters:' + kind)) || 'null');
      const next = { ...filters, ...saved?.draft, ...initialFilters };
      if (/^平台状态|^未知推广状态/.test(next.status || '')) next.status = '';
      if (shops.length) next.shop_id = validSelection(next.shop_id, shops);
      if (platform === 'taobao' && next.trade_range === 'quarter') next.trade_range = 'month';
      setFilters(next);
      setDraft(next.keyword || '');
    } catch {}
    setReady(true);
  }, []);
  const previousPlatform = useRef(platform);
  useQueryLocation(kind, { ...filters, platform }, ready);
  useEffect(() => {
    if (previousPlatform.current === platform) return;
    previousPlatform.current = platform;
    const next = { ...filters, shop_id: validSelection(filters.shop_id, shops) };
    if (platform === 'taobao' && next.trade_range === 'quarter') next.trade_range = 'month';
    setFilters(next);
    setPage(1);
  }, [platform]);
  useEffect(() => {
    const timer = setTimeout(() => {
      setFilters((f) => (f.keyword === draft ? f : { ...f, keyword: draft }));
      setPage(1);
    }, 350);
    return () => clearTimeout(timer);
  }, [draft]);
  useEffect(() => {
    if (ready)
      try {
        localStorage.setItem(preferenceKey('pdd-manager:filters:' + kind), JSON.stringify({ draft: filters }));
      } catch {}
  }, [filters, kind, ready]);
  const { data, error, loading, refresh } = useReportData(
    kind,
    { ...filters, platform, page, page_size: pageSize },
    ready,
  );
  useEffect(() => {
    if (data?.page_count && page > data.page_count) setPage(data.page_count);
  }, [data?.page_count, page]);
  const change = (key, value) => {
    setFilters((f) => ({ ...f, [key]: value }));
    setPage(1);
    setDailyPage(1);
  };
  const selectedPlatform = scopePlatform(shops, filters.shop_id, platform);
  const onlyTaobao =
    shops.filter((s) => inScope(filters.shop_id, s.id)).length > 0 &&
    shops.filter((s) => inScope(filters.shop_id, s.id)).every((s) => s.platform === 'taobao');
  useEffect(() => {
    if (onlyTaobao && filters.trade_range === 'quarter') change('trade_range', 'month');
  }, [onlyTaobao, filters.trade_range]);
  const rows = data?.rows || [],
    metrics = data?.metrics || [],
    sources = data?.sources || [],
    totals = data?.ad_totals || {};
  const platformNotes = [...new Set(sources.map((s) => s.scope_note).filter(Boolean))].map((note) => (
    <p className="data-note" key={note}>
      {note}
    </p>
  ));
  const daily = useMemo(
      () =>
        sortedRows(
          (data?.metrics || [])
            .flatMap((m) =>
              (m.daily || []).map((r) => ({
                ...r,
                shop_id: m.shop_id,
                shop_name: m.shop_name,
                shop_label: shopLabel(m),
                operators: shops.find((s) => s.id === m.shop_id)?.operators || [],
              })),
            )
            .sort((a, b) => b.date.localeCompare(a.date) || a.shop_id - b.shop_id),
          dailySort,
        ),
      [data, dailySort, shops],
    ),
    dailyPages = Math.max(1, Math.ceil(daily.length / pageSize)),
    currentDaily = Math.min(dailyPage, dailyPages);
  return (
    <section className="business-panel promotions-panel" ref={root}>
      <div className="business-heading">
        <div>
          <h2>{kind === 'ad_products' ? '商品推广' : '推广报表'}</h2>
          <p>
            已读取 {metrics.length} / {sources.length || shops.length} 家店铺
          </p>
        </div>
      </div>
      <div className="filter-panel">
        <PlatformFilter />
        <label>
          店铺
          <MultiSelect
            label="推广店铺"
            value={filters.shop_id}
            onChange={(value) => change('shop_id', value)}
            options={shops.map((s) => ({ value: s.id, label: shopLabel(s) }))}
            placeholder="全部店铺"
          />
        </label>
        <label>
          统计范围
          <Choice
            aria-label="推广统计范围"
            value={filters.trade_range}
            onChange={(e) => change('trade_range', e.target.value)}
          >
            {[
              ['realtime', '今日'],
              ['yesterday', '昨日'],
              ['week', '近 7 日'],
              ['month', '近 30 日'],
              ['quarter', '近 90 日'],
            ]
              .filter(([value]) => value !== 'quarter' || !onlyTaobao)
              .map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
          </Choice>
        </label>
        <label>
          商品 / 计划 / 场景
          <TextInput
            aria-label="推广商品搜索"
            placeholder="名称 / 商品或推广编号 / 运营"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
        </label>
        <label>
          推广状态
          <Choice aria-label="推广状态" value={filters.status} onChange={(e) => change('status', e.target.value)}>
            <option value="">全部状态</option>
            {(data?.statuses || []).map((s) => (
              <option key={s}>{s}</option>
            ))}
          </Choice>
        </label>
      </div>
      <div className="workspace-controls">
        <SavedFilters
          scope={kind}
          draft={filters}
          pageSize={pageSize}
          onApply={(item) => {
            setFilters(item.draft);
            setDraft(item.draft.keyword || '');
            if ([20, 50, 100].includes(item.pageSize)) setPageSize(item.pageSize);
            setPage(1);
          }}
        />
        <ExportButton kind={kind} filters={{ ...filters, platform }} disabled={!data} />
        <TablePreferences root={root} scope={kind} />
      </div>
      <div className="table-update-status" role="status" aria-live="polite">
        {loading ? (data ? '正在更新排序 / 数据，当前记录保留显示…' : '正在读取数据…') : ''}
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <DataCoverage sources={sources} onRefresh={refresh} />
      <OnDemandDetails sources={sources} label="商品推广明细" onCollect={() => refresh(true)} busy={loading} />
      <CollapsibleSection className="data-module">
        <h3>推广概况</h3>
        <div className="promotion-metrics">
          {adFields
            .filter(([k]) => ['ad_spend', 'ad_revenue', 'roi', 'net_roi'].includes(k))
            .map(([key, label, format]) => (
              <article key={key}>
                <span>
                  {label}
                  {key.includes('roi') ? '（ROI）' : '（元）'}
                </span>
                <strong>{format(totals[key])}</strong>
              </article>
            ))}
        </div>
        <details className="context-note">
          <summary>统计说明</summary>
          <p>
            实际投产比 = 推广交易额 ÷ 成交花费；净实际投产比 = 净交易额 ÷ 成交花费。花费为 0 时显示
            —。概况按店铺及统计范围汇总，商品筛选仅影响下方明细。近 7／30／90 日截至昨日。
          </p>
        </details>
        {selectedPlatform && platformNotes}
        {sources.some((s) => s.error || s.stale || !s.updated_at) && (
          <p className="data-note">部分店铺待更新，汇总包含上次记录。</p>
        )}
      </CollapsibleSection>
      <CollapsibleSection className="data-module">
        <div className="table-caption">
          <strong>推广明细 · {data?.total ?? 0} 条</strong>
          <span>
            {data?.scope?.date_from} 至 {data?.scope?.date_to}
          </span>
        </div>
        <div className="data-table-wrap">
          <SortableTable
            className="data-table"
            serverSort={{
              fields: [
                'shop_name',
                'name',
                'status',
                ...(kind === 'ad_products' ? ['target_roi'] : []),
                ...adFields.map(([k]) => k),
              ],
              field: filters.sort_by,
              direction: filters.sort_direction,
              onChange: (field, direction) => {
                setFilters((f) => ({ ...f, sort_by: field, sort_direction: direction }));
                setPage(1);
              },
            }}
          >
            <thead>
              <tr>
                {[
                  '店铺',
                  '商品 / 计划 / 场景',
                  '状态 / 类型',
                  ...(kind === 'ad_products' ? ['目标投产比 / 预算'] : []),
                  ...adFields.map(([, label]) => label),
                  '操作',
                ].map((label) => (
                  <th key={label}>{label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr data-shop-id={row.shop_id} key={`${row.shop_id}:${row.id}`}>
                  <td>{shopLabel(row)}</td>
                  <td className="wide-cell">
                    <ProductInfo row={row} />
                    <small>
                      {row.row_type === 'promotion_scene' ? '场景编号' : '推广编号'} {row.id}
                    </small>
                    {row.campaign_name && <small>计划：{row.campaign_name}</small>}
                  </td>
                  <td>
                    <StatusBadge value={row.status} />
                    <small>{row.promotion_type}</small>
                  </td>
                  {kind === 'ad_products' && (
                    <td>
                      {num(row.target_roi)}
                      {row.target_roi_label && <small>{row.target_roi_label}</small>}
                      <small>日预算：{row.budget_text || money(row.budget)}</small>
                    </td>
                  )}
                  {adFields.map(([key, , format]) => (
                    <td key={key} className="finance-amount">
                      {format(row[key])}
                    </td>
                  ))}
                  <td>
                    <NativeButton className="table-action" onClick={() => setDetail(row)}>
                      指标详情
                    </NativeButton>
                    <NativeButton className="table-action" onClick={() => onOpen(row.shop_id, kind)}>
                      查看后台
                    </NativeButton>
                  </td>
                </tr>
              ))}
            </tbody>
          </SortableTable>
          {!rows.length && sources.some((source) => source.details_on_demand && !source.details_updated_at) ? (
            <p className="table-empty">汇总已更新，商品推广明细尚未采集，可点击上方按钮按需采集。</p>
          ) : (
            !rows.length && (
              <EmptyData
                loading={loading}
                error={error}
                sources={sources}
                onRetry={() => refresh(false)}
                onSync={() => refresh(true)}
              />
            )
          )}
        </div>
        <div className="pagination">
          <span>
            第 {page} / {data?.page_count || 1} 页
          </span>
          <Choice
            aria-label="推广每页数量"
            value={pageSize}
            onChange={(e) => {
              setPageSize(Number(e.target.value));
              setPage(1);
              setDailyPage(1);
            }}
          >
            {[20, 50, 100].map((n) => (
              <option key={n} value={n}>
                {n} 条/页
              </option>
            ))}
          </Choice>
          <label>
            跳至{' '}
            <TextInput
              aria-label="推广明细跳页"
              type="number"
              min="1"
              max={data?.page_count || 1}
              value={page}
              onChange={(e) => setPage(Math.max(1, Math.min(data?.page_count || 1, Number(e.target.value) || 1)))}
            />
          </label>
          <NativeButton disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            上一页
          </NativeButton>
          <NativeButton disabled={page >= (data?.page_count || 1)} onClick={() => setPage((p) => p + 1)}>
            下一页
          </NativeButton>
        </div>
      </CollapsibleSection>
      {kind === 'ad_reports' && (
        <CollapsibleSection className="promotion-daily">
          <div className="table-caption">
            <strong>分日报表 · {daily.length} 条</strong>
            <span>保留各店铺日期，支持横向查看完整指标</span>
          </div>
          <div className="data-table-wrap">
            <SortableTable
              className="data-table"
              serverSort={{
                fields: ['shop_name', 'date', ...adFields.map(([k]) => k)],
                ...dailySort,
                onChange: (field, direction) => {
                  setDailySort({ field, direction });
                  setDailyPage(1);
                },
              }}
            >
              <thead>
                <tr>
                  {['店铺', '统计日期', ...adFields.map(([, label]) => label)].map((s) => (
                    <th key={s}>{s}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {daily.slice((currentDaily - 1) * pageSize, currentDaily * pageSize).map((row) => (
                  <tr data-shop-id={row.shop_id} key={`${row.shop_id}:${row.date}`}>
                    <td>{shopLabel(row)}</td>
                    <td>{row.date}</td>
                    {adFields.map(([key, , format]) => (
                      <td className="finance-amount" key={key}>
                        {format(row[key])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </SortableTable>
            {!daily.length && <p className="table-empty">平台分日数据尚未读取</p>}
          </div>
          <div className="pagination">
            <span>
              第 {currentDaily} / {dailyPages} 页
            </span>
            <label>
              跳至{' '}
              <TextInput
                type="number"
                aria-label="分日报表跳页"
                min="1"
                max={dailyPages}
                value={currentDaily}
                onChange={(e) => setDailyPage(Math.max(1, Math.min(dailyPages, Number(e.target.value) || 1)))}
              />
            </label>
            <NativeButton disabled={currentDaily <= 1} onClick={() => setDailyPage(currentDaily - 1)}>
              上一页
            </NativeButton>
            <NativeButton disabled={currentDaily >= dailyPages} onClick={() => setDailyPage(currentDaily + 1)}>
              下一页
            </NativeButton>
          </div>
        </CollapsibleSection>
      )}
      {detail && (
        <AdminDialog>
          <section className="admin-dialog-content record-dialog">
            <div className="dialog-head">
              <h2>推广指标详情</h2>
              <NativeButton onClick={() => setDetail(null)}>关闭</NativeButton>
            </div>
            <p>
              {shopLabel(detail)} · {detail.name}
            </p>
            <dl>
              {adFields.map(([k, label, format]) => (
                <div key={k}>
                  <dt>{label}</dt>
                  <dd>{format(detail[k])}</dd>
                </div>
              ))}
            </dl>
          </section>
        </AdminDialog>
      )}
      {!selectedPlatform && platformNotes}
    </section>
  );
}
