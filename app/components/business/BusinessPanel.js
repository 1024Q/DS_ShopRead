'use client';
import AdminDialog from '../shared/AdminDialog';
import { NativeButton, TextInput, Choice } from '../shared/AdminControls';
import { preferenceKey } from '../../lib/preference-scope.mjs';
import EmptyData from '../shared/EmptyData';
import InvoiceDetail from './InvoiceDetail';
import InvoiceImport from '../reconciliation/InvoiceImport';
import useQueryLocation from '../../hooks/useQueryLocation';
import MultiSelect from '../shared/MultiSelect';
import { validSelection, scopePlatform } from '../../lib/filter-scope.mjs';
import StatusBadge from '../shared/StatusBadge';
import { DEFAULT_STATUSES, restoreStatus } from '../../lib/status-tone.mjs';
import { pickupCountdown } from '../../lib/package-countdown.mjs';
import DateCollection from '../collection/DateCollection';
import DataCoverage from '../collection/DataCoverage';
import CollapsibleSection from '../shared/CollapsibleSection';
import PlatformFilter from '../shared/PlatformFilter';
import { databaseReader, dataScope } from '../../lib/database-reader';
import { shopLabel } from '../../lib/shop-identity.mjs';
import SortableTable from '../shared/SortableTable';
import { TaxSubjects, TaxSummary, TaxCells, TaxDetail, chineseDate, taxHeaders } from './TaxInfo';
import FinancialBreakdown from '../overview/FinancialBreakdown';
import ActivityDetail from './ActivityDetail';
import TransactionData from './TransactionData';
import { fieldsByKind } from '../../lib/table-sort.mjs';
import GoodsData from './GoodsData';
import OverviewDashboard from '../overview/OverviewDashboard';
import { canReadScope } from '../../lib/platforms.mjs';
import { useWorkspace, useGlobalRefresh, useDatabaseUpdates } from '../../providers/WorkspaceContext';
import { ProductInfo } from '../shared/ProductImage';
import { SavedFilters, TablePreferences, ExportButton } from '../shell/WorkspaceTools';
import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import OperationCells, { operationTitles, operationStatuses, operationHeaders, operationNotes } from './OperationCells';

const titles = {
  ...operationTitles,
  goodsdata: '商品数据',
  transactions: '交易数据',
  overview: '实时概览',
  products: '商品列表',
  tickets: '工单列表',
  reviews: '评价管理',
};
const number = (value) => (value == null ? '—' : Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 }));
const money = (value) =>
  value == null
    ? '—'
    : '¥' + Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
function day(offset = 0) {
  return new Date(Date.now() + 8 * 3600000 + offset * 86400000).toISOString().slice(0, 10);
}
function cleanFilters(kind, value) {
  return {
    ...value,
    ...(kind === 'orders' && !value.status ? { status: '待发货' } : {}),
    ...(kind === 'products' ? { min_price: '', max_price: '' } : {}),
    ...(kind === 'activities' ? { date_from: '', date_to: '', exact_dates: false } : {}),
  };
}
function defaults(kind) {
  const days = { reviews: 3, tickets: 7, packages: 3, evidence: 180, intercepts: 90 }[kind];
  return {
    exact_dates: false,
    sort_by: kind === 'goodsdata' ? 'visitors' : '',
    sort_direction: kind === 'goodsdata' ? 'desc' : '',
    trade_range: ['transactions', 'goodsdata'].includes(kind) ? 'realtime' : '',
    shop_id: '',
    keyword: '',
    activity_type: '',
    activity_group: '',
    brand_reason: '',
    status: DEFAULT_STATUSES[kind] || '',
    stars: [],
    reply: '',
    date_from: days ? day(1 - days) : '',
    date_to: days ? day() : '',
    min_price: '',
    max_price: '',
    low_stock: '',
    priority_only: false,
    carrier: '',
    subject: '',
    period:
      kind === 'tax'
        ? day().slice(0, 7)
        : kind === 'reconciliation_day'
          ? day().slice(0, 7)
          : kind === 'reconciliation_month'
            ? day().slice(0, 4)
            : '',
  };
}

export default function BusinessPanel({ kind, shops, onOpen, onChangeKind, initialFilters, platform = '' }) {
  const [draft, setDraft] = useState(() => defaults(kind)),
    [filters, setFilters] = useState(() => defaults(kind));
  const [clockNow, setClockNow] = useState(() => Date.now());
  useEffect(() => {
    if (kind !== 'packages') return;
    const timer = setInterval(() => setClockNow(Date.now()), 60000);
    return () => clearInterval(timer);
  }, [kind]);
  const [saved, setSaved] = useState(null),
    [error, setError] = useState('');
  const { auto, autoReady, live, businessDay, pauseVersion } = useWorkspace();
  const lastBusinessDay = useRef(businessDay);
  const [filtersReady, setFiltersReady] = useState(false);
  useQueryLocation(kind, { ...filters, platform }, filtersReady);
  const [knownSubjects, setKnownSubjects] = useState([]);
  const [expanded, setExpanded] = useState({});
  const [page, setPage] = useState(1),
    [pageSize, setPageSize] = useState(20),
    [detail, setDetail] = useState(null),
    [loading, setLoading] = useState(false);
  const refreshRef = useRef(null),
    currentData = useRef(null),
    root = useRef(null);
  const tableSort = {
    fields: fieldsByKind[kind] || [],
    field: draft.sort_by,
    direction: draft.sort_direction,
    onChange: (field, direction) => {
      const next = { ...draft, sort_by: field, sort_direction: direction };
      setDraft(next);
      setFilters(next);
      setPage(1);
    },
  };
  const readable = canReadScope(shops, filters.shop_id, kind);
  const displayReadable = canReadScope(shops, draft.shop_id, kind);
  const query = JSON.stringify({
    ...filters,
    platform,
    page,
    page_size: pageSize,
    ...(kind === 'overview' ? { business_day: businessDay } : {}),
  });
  const scope =
    kind +
    ':' +
    (kind === 'overview' ? businessDay + ':' : '') +
    dataScope(new URLSearchParams({ ...filters, platform }));
  const data = saved?.scope === scope ? saved.result : null;
  const read = useMemo(() => databaseReader(`/api/data/${kind}`), [kind]);
  const updating = loading || (!error && saved?.query !== query);
  useEffect(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(preferenceKey('pdd-manager:filters:' + kind)) || 'null');
      const next = { ...defaults(kind), ...restoreStatus(kind, saved, initialFilters) };
      if (platform && platform !== 'pdd') next.exact_dates = false;
      if (shops.length) next.shop_id = validSelection(next.shop_id, shops);
      setDraft(cleanFilters(kind, next));
      setFilters(cleanFilters(kind, next));
      if ([20, 50, 100].includes(saved?.pageSize)) setPageSize(saved.pageSize);
    } catch {
      if (initialFilters) {
        const next = { ...defaults(kind), ...initialFilters };
        setDraft(cleanFilters(kind, next));
        setFilters(cleanFilters(kind, next));
      }
    }
    setFiltersReady(true);
  }, []);
  const previousPlatform = useRef(platform);
  useEffect(() => {
    if (previousPlatform.current === platform) return;
    previousPlatform.current = platform;
    const next = {
      ...draft,
      shop_id: validSelection(draft.shop_id, shops),
      ...(platform && platform !== 'pdd' ? { exact_dates: false } : {}),
    };
    setDraft(next);
    setFilters(next);
    setPage(1);
  }, [platform]);
  useGlobalRefresh((mode) => refreshRef.current?.(mode === 'collect', mode === 'collect'));
  useDatabaseUpdates(() => refreshRef.current?.(false), { kind, shopId: filters.shop_id, platform });
  useEffect(() => {
    if (!autoReady || !filtersReady) return;
    if (!readable) {
      setSaved(null);
      setError('');
      setLoading(false);
      refreshRef.current = null;
      currentData.current = null;
      return;
    }
    let active = true,
      fetching = false,
      queuedForce = false,
      queuedRead = false,
      last = 0;
    setError('');
    const controller = new AbortController();
    const load = async (force = false, registerMissing = true) => {
      if (fetching) {
        queuedForce ||= force;
        queuedRead ||= !force;
        return;
      }
      force ||= queuedForce;
      queuedForce = false;
      queuedRead = false;
      const generation = pauseVersion.current;
      fetching = true;
      setLoading(true);
      try {
        const values = JSON.parse(query),
          params = new URLSearchParams();
        for (const [key, value] of Object.entries(values))
          if (value !== '' && (!Array.isArray(value) || value.length))
            params.set(key, Array.isArray(value) ? value.join(',') : value);
        const response = await read(params, {
          force,
          registerMissing,
          signal: controller.signal,
          onPreview: (result) => {
            if (active && generation === pauseVersion.current && !currentData.current)
              setSaved({ scope, query, result });
          },
        });
        const result = await response.json();
        if (!response.ok)
          throw Error(typeof result.detail === 'string' ? result.detail : '筛选条件无效，请检查日期和数值范围');
        if (active && generation === pauseVersion.current) {
          if (result.subjects?.length) setKnownSubjects(result.subjects);
          if (result.page_count && page > result.page_count) setPage(result.page_count);
          setSaved({ scope, query, result });
          setDetail((previous) =>
            previous
              ? (result.rows || []).find(
                  (r) =>
                    String(r.id) === String(previous.id) &&
                    r.shop_id === previous.shop_id &&
                    (kind !== 'tax' || r.period === previous.period),
                ) || (kind === 'tax' ? previous : null)
              : null,
          );
          currentData.current = result;
          setError('');
        }
        last = Date.now();
      } catch (e) {
        if (active && e.name !== 'AbortError') setError(e.message);
      } finally {
        fetching = false;
        if (active) {
          setLoading(false);
          if (queuedForce || queuedRead) queueMicrotask(() => load(queuedForce));
        }
      }
    };
    const crossedDay = kind === 'overview' && lastBusinessDay.current !== businessDay;
    lastBusinessDay.current = businessDay;
    refreshRef.current = load;
    currentData.current = null;
    load();

    return () => {
      active = false;
      controller.abort();
      refreshRef.current = null;
    };
  }, [kind, read, scope, query, autoReady, filtersReady, readable, businessDay]);
  useEffect(() => {
    if (draft.date_from && draft.date_to && draft.date_from > draft.date_to) return;
    if (!autoReady || !filtersReady || JSON.stringify(draft) === JSON.stringify(filters)) return;
    const timer = setTimeout(() => {
      setFilters({ ...draft });
      setPage(1);
      setDetail(null);
      setExpanded({});
    }, 400);
    return () => clearTimeout(timer);
  }, [draft, filters, autoReady, filtersReady]);
  useEffect(() => {
    if (autoReady && filtersReady)
      try {
        localStorage.setItem(
          preferenceKey('pdd-manager:filters:' + kind),
          JSON.stringify({ draft, pageSize, statusDefaultsVersion: 1 }),
        );
      } catch {}
  }, [draft, pageSize, kind, autoReady, filtersReady]);
  useEffect(() => {
    const update = () => refreshRef.current?.();
    window.addEventListener('tax-review-updated', update);
    return () => window.removeEventListener('tax-review-updated', update);
  }, []);
  const change = (key, value) => {
    const next = { ...draft, [key]: value };
    if (kind === 'activities' && key === 'status') next.activity_group = '';
    if (['date_from', 'date_to'].includes(key) && !value) next.exact_dates = false;
    setDraft(next);
    if (
      !(next.date_from && next.date_to && next.date_from > next.date_to) &&
      (key === 'shop_id' || key === 'sort_by' || key === 'sort_direction')
    ) {
      setFilters(next);
      setPage(1);
    }
  };
  const apply = (e) => {
    e.preventDefault();
    if (draft.date_from && draft.date_to && draft.date_from > draft.date_to) return;
    setPage(1);
    setFilters({ ...draft });
  };
  const reset = () => {
    const value = defaults(kind);
    setDraft(value);
    setFilters(value);
    setPage(1);
  };
  const sources = data?.sources || [],
    syncing = sources.some((source) => source.syncing),
    ready = sources.filter((source) => source.updated_at).length;
  const rows = data?.rows || [],
    isOperation = !!operationTitles[kind],
    isFinance = kind.startsWith('reconciliation_'),
    isTax = kind === 'tax',
    isMoney = isFinance || isTax;
  const selectedPlatform = scopePlatform(shops, filters.shop_id, platform);
  const platformNotes = [
    ...new Set(
      sources
        .filter((s) => s.platform === 'taobao')
        .map((s) => s.scope_note)
        .filter(Boolean),
    ),
  ].map((note) => (
    <p className="data-note platform-scope-note" key={note}>
      {note}
    </p>
  ));
  const taobaoOnly = scopePlatform(shops, draft.shop_id, platform) === 'taobao';
  const statusOptions = [
    ...new Set(
      kind === 'products'
        ? ['在售中', '已下架', '已售罄']
        : kind === 'tickets'
          ? ['待处理', '处理中', '2小时内将逾期', '已完结', '已关闭', '违规已处理']
          : [
              ...(taobaoOnly && ['aftersales', 'packages', 'invoices'].includes(kind)
                ? []
                : operationStatuses[kind] || []),
              ...(data?.statuses || []),
            ],
    ),
  ];
  if (kind === 'overview')
    return (
      <OverviewDashboard
        platform={platform}
        data={displayReadable ? data : null}
        shops={shops}
        shopId={draft.shop_id}
        onShopChange={(value) => change('shop_id', value)}
        auto={auto}
        autoReady={autoReady && filtersReady}
        loading={loading}
        syncing={syncing}
        error={error}
        onRefresh={() => refreshRef.current?.(true)}
        onNavigate={onChangeKind}
        onOpen={onOpen}
        tools={
          <>
            <SavedFilters
              scope={kind}
              draft={draft}
              pageSize={pageSize}
              onApply={(item) => setDraft({ ...defaults(kind), ...item.draft })}
            />
            <ExportButton
              kind={kind}
              filters={{ ...filters, platform }}
              disabled={!displayReadable || !data || JSON.stringify(draft) !== JSON.stringify(filters)}
            />
          </>
        }
      />
    );
  return (
    <section className="business-panel" ref={root}>
      <div className="table-update-status" role="status" aria-live="polite">
        {updating ? (data ? '正在更新排序 / 数据，当前记录保留显示…' : '正在读取数据…') : ''}
      </div>
      <div className="business-heading">
        <div>
          <h2>{titles[kind]}</h2>
        </div>
      </div>
      {kind === 'orders' && (
        <nav className="activity-quick-filters order-query-tabs" aria-label="订单分类">
          {[
            ['待付款', '待付款'],
            ['待发货', '待发货'],
            ['待收货', '待收货'],
            ['退款/售后', '退款/售后'],
          ].map(([value, label]) => (
            <NativeButton
              type="button"
              key={value}
              aria-pressed={draft.status === value}
              onClick={() => change('status', value)}
            >
              {label}
            </NativeButton>
          ))}
          <NativeButton
            type="button"
            aria-pressed={draft.status === '即将逾期发货'}
            onClick={() => change('status', '即将逾期发货')}
          >
            即将逾期发货
          </NativeButton>
        </nav>
      )}
      {isFinance && (
        <nav className="sub-tabs" aria-label="对账汇总方式">
          {[
            ['reconciliation_day', '日汇总'],
            ['reconciliation_month', '月汇总'],
          ].map(([key, label]) => (
            <NativeButton
              key={key}
              type="button"
              className={kind === key ? 'primary' : ''}
              aria-pressed={kind === key}
              onClick={() => onChangeKind?.(key)}
            >
              {label}
            </NativeButton>
          ))}
        </nav>
      )}
      <form className="filter-panel" onSubmit={apply}>
        <PlatformFilter />
        {isTax ? (
          <label>
            主体
            <Choice aria-label="主体" value={draft.subject} onChange={(e) => change('subject', e.target.value)}>
              <option value="">全部主体</option>
              {(data?.subjects?.length ? data.subjects : knownSubjects).map((subject) => (
                <option key={subject.id} value={subject.id}>
                  {subject.name}（{subject.entity.credit_code || '信用代码未提供'}）
                </option>
              ))}
              {draft.subject &&
                !(data?.subjects?.length ? data.subjects : knownSubjects).some((s) => s.id === draft.subject) && (
                  <option value={draft.subject}>已选择的主体（读取中）</option>
                )}
            </Choice>
          </label>
        ) : (
          <label>
            店铺
            <MultiSelect
              label="店铺筛选"
              value={draft.shop_id}
              onChange={(value) => change('shop_id', value)}
              options={shops.map((s) => ({ value: s.id, label: shopLabel(s) }))}
              placeholder="全部店铺"
            />
          </label>
        )}
        {isTax && (
          <>
            <label>
              查询方式
              <Choice
                aria-label="查询方式"
                value={draft.period.includes('Q') ? 'quarter' : 'month'}
                onChange={(e) =>
                  change(
                    'period',
                    e.target.value === 'quarter'
                      ? draft.period.slice(0, 4) + '-Q' + Math.ceil(Number(draft.period.slice(5)) / 3)
                      : draft.period.slice(0, 4) +
                          '-' +
                          String((Number(draft.period.slice(-1)) - 1) * 3 + 1).padStart(2, '0'),
                  )
                }
              >
                <option value="month">按月份</option>
                <option value="quarter">按季度</option>
              </Choice>
            </label>
            <label>
              年份
              <Choice
                aria-label="入账年份"
                value={draft.period.slice(0, 4)}
                onChange={(e) => {
                  const year = e.target.value;
                  const current = year === day().slice(0, 4);
                  const quarter = draft.period.includes('Q');
                  const value = Math.min(
                    Number(quarter ? draft.period.slice(-1) : draft.period.slice(5)),
                    current
                      ? quarter
                        ? Math.ceil(Number(day().slice(5, 7)) / 3)
                        : Number(day().slice(5, 7))
                      : quarter
                        ? 4
                        : 12,
                  );
                  change('period', year + '-' + (quarter ? 'Q' + value : String(value).padStart(2, '0')));
                }}
              >
                {Array.from({ length: Number(day().slice(0, 4)) - 1999 }, (_, i) => Number(day().slice(0, 4)) - i).map(
                  (year) => (
                    <option key={year} value={year}>
                      {year}年
                    </option>
                  ),
                )}
              </Choice>
            </label>
            {draft.period.includes('Q') ? (
              <label>
                入账季度
                <Choice
                  aria-label="入账季度"
                  value={draft.period.slice(-1)}
                  onChange={(e) => change('period', draft.period.slice(0, 4) + '-Q' + e.target.value)}
                >
                  {[1, 2, 3, 4].map((q) => (
                    <option
                      key={q}
                      value={q}
                      disabled={
                        draft.period.slice(0, 4) === day().slice(0, 4) && q > Math.ceil(Number(day().slice(5, 7)) / 3)
                      }
                    >
                      第{['一', '二', '三', '四'][q - 1]}季度（{q * 3 - 2}—{q * 3}月）
                    </option>
                  ))}
                </Choice>
              </label>
            ) : (
              <label>
                入账月份
                <Choice
                  aria-label="入账月份"
                  value={Number(draft.period.slice(5))}
                  onChange={(e) => change('period', draft.period.slice(0, 4) + '-' + e.target.value.padStart(2, '0'))}
                >
                  {Array.from({ length: 12 }, (_, i) => i + 1).map((month) => (
                    <option
                      key={month}
                      value={month}
                      disabled={draft.period.slice(0, 4) === day().slice(0, 4) && month > Number(day().slice(5, 7))}
                    >
                      {month}月
                    </option>
                  ))}
                </Choice>
              </label>
            )}
          </>
        )}
        {isFinance && (
          <label>
            {kind === 'reconciliation_day' ? '入账月份' : '入账年份'}
            <TextInput
              aria-label={kind === 'reconciliation_day' ? '入账月份' : '入账年份'}
              type={kind === 'reconciliation_day' ? 'month' : 'number'}
              min={kind === 'reconciliation_day' ? '2000-01' : '2000'}
              max={kind === 'reconciliation_day' ? day().slice(0, 7) : day().slice(0, 4)}
              required
              value={draft.period}
              onChange={(e) => change('period', e.target.value)}
            />
          </label>
        )}
        {['transactions', 'goodsdata'].includes(kind) && (
          <label>
            统计范围
            <Choice
              aria-label={kind === 'goodsdata' ? '商品统计范围' : '交易统计范围'}
              value={draft.trade_range}
              onChange={(e) => change('trade_range', e.target.value)}
            >
              <option value="realtime">实时（今日）</option>
              <option value="yesterday">昨日</option>
              <option value="week">近 7 日</option>
              <option value="month">近 30 日</option>
            </Choice>
          </label>
        )}
        {kind !== 'overview' && !['transactions', 'goodsdata'].includes(kind) && !isMoney && (
          <>
            <label className="keyword-filter">
              关键词
              <TextInput
                value={draft.keyword}
                onChange={(e) => change('keyword', e.target.value)}
                maxLength={200}
                placeholder={
                  kind === 'ad_logs'
                    ? '操作人 / 操作对象 / 操作详情 / 运营'
                    : kind === 'activities'
                      ? '活动名称 / 活动 ID / 商品 / 报名编号 / 运营'
                      : isOperation
                        ? '商品 / 订单 / 运单 / 问题 / 运营'
                        : kind === 'products'
                          ? '商品名称 / ID / 编码 / 运营'
                          : kind === 'tickets'
                            ? '订单号 / 工单号 / 问题 / 商品 / 运营'
                            : '订单号 / 商品 ID / 评价内容 / 运营'
                }
              />
            </label>
            {(kind === 'products' || kind === 'tickets' || (isOperation && kind !== 'orders')) && (
              <label>
                {kind === 'ad_logs' ? '操作类型' : kind === 'activities' ? '更多状态' : '状态'}
                <Choice value={draft.status} onChange={(e) => change('status', e.target.value)}>
                  <option value="">全部状态</option>
                  {statusOptions.map((status) => (
                    <option key={status}>{status}</option>
                  ))}
                </Choice>
              </label>
            )}
            {!['navigator', 'quality', 'brands', 'activities', 'ad_logs'].includes(kind) && (
              <>
                <label>
                  {kind === 'products'
                    ? '创建日期起'
                    : kind === 'intercepts'
                      ? '下单日期起'
                      : kind === 'packages'
                        ? '发货日期起'
                        : kind === 'activities'
                          ? '提交日期起'
                          : '开始日期'}
                  <TextInput
                    type="date"
                    max={draft.date_to || undefined}
                    value={draft.date_from}
                    onChange={(e) => change('date_from', e.target.value)}
                  />
                </label>
                <label>
                  结束日期
                  <TextInput
                    type="date"
                    min={draft.date_from || undefined}
                    value={draft.date_to}
                    onChange={(e) => change('date_to', e.target.value)}
                  />
                </label>
              </>
            )}
            {!['products', 'navigator', 'quality', 'brands', 'activities', 'ad_logs'].includes(kind) && (
              <label>
                快捷日期
                <Choice
                  value={
                    draft.date_to === day()
                      ? String([1, 3, 7, 30, 90, 180].find((n) => draft.date_from === day(1 - n)) || '')
                      : ''
                  }
                  onChange={(e) => {
                    if (e.target.value)
                      setDraft((previous) => ({
                        ...previous,
                        date_from: day(1 - Number(e.target.value)),
                        date_to: day(),
                      }));
                  }}
                >
                  <option value="">选择范围</option>
                  <option value="1">今天</option>
                  <option value="3">近三天</option>
                  <option value="7">近七天</option>
                  <option value="30">近三十天</option>
                  <option value="90">近九十天</option>
                  {kind !== 'orders' && <option value="180">近一百八十天</option>}
                </Choice>
              </label>
            )}
            {kind === 'products' && (
              <>
                <label>
                  库存不超过
                  <TextInput
                    type="number"
                    min="0"
                    placeholder="不限"
                    value={draft.low_stock}
                    onChange={(e) => change('low_stock', e.target.value)}
                  />
                </label>
              </>
            )}
            {kind === 'reviews' && (
              <>
                <label>
                  商家回复
                  <Choice value={draft.reply} onChange={(e) => change('reply', e.target.value)}>
                    <option value="">全部</option>
                    <option value="unreplied">未回复</option>
                    <option value="replied">已回复</option>
                  </Choice>
                </label>
                <fieldset className="star-filter">
                  <legend>用户评价得分</legend>
                  {[1, 2, 3, 4, 5].map((star) => (
                    <label key={star}>
                      <TextInput
                        type="checkbox"
                        checked={draft.stars.includes(star)}
                        onChange={(e) =>
                          change(
                            'stars',
                            e.target.checked ? [...draft.stars, star] : draft.stars.filter((s) => s !== star),
                          )
                        }
                      />
                      {star} 星
                    </label>
                  ))}
                </fieldset>
              </>
            )}
          </>
        )}
        {kind === 'ad_logs' && (
          <label>
            操作日期
            <Choice
              aria-label="操作记录快捷日期"
              value={
                draft.date_from === day()
                  ? 'today'
                  : draft.date_from === day(-1) && draft.date_to === day(-1)
                    ? 'yesterday'
                    : draft.date_from === day(-6)
                      ? 'week'
                      : ''
              }
              onChange={(e) => {
                const v = e.target.value;
                setDraft((f) => ({
                  ...f,
                  date_from: v === 'today' ? day() : v === 'yesterday' ? day(-1) : v === 'week' ? day(-6) : '',
                  date_to: v === 'yesterday' ? day(-1) : v ? day() : '',
                }));
              }}
            >
              <option value="">平台可用范围</option>
              <option value="today">今日</option>
              <option value="yesterday">昨日</option>
              <option value="week">近七日</option>
            </Choice>
          </label>
        )}
        {kind === 'brands' && (
          <label>
            无黑标原因
            <Choice
              aria-label="无黑标原因"
              value={draft.brand_reason}
              onChange={(e) => change('brand_reason', e.target.value)}
            >
              <option value="">全部原因</option>
              {(data?.brand_reasons || []).map((reason) => (
                <option key={reason}>{reason}</option>
              ))}
              {draft.brand_reason && !data?.brand_reasons?.includes(draft.brand_reason) && (
                <option>{draft.brand_reason}</option>
              )}
            </Choice>
          </label>
        )}
        {kind === 'activities' && (
          <label>
            活动类型
            <Choice
              aria-label="活动类型"
              value={draft.activity_type}
              onChange={(e) => change('activity_type', e.target.value)}
            >
              <option value="">全部类型</option>
              {(data?.activity_types || []).map((t) => (
                <option key={t}>{t}</option>
              ))}
              {draft.activity_type && !data?.activity_types?.includes(draft.activity_type) && (
                <option value={draft.activity_type}>{draft.activity_type}</option>
              )}
            </Choice>
          </label>
        )}
        {kind === 'goodsdata' && (
          <>
            <label className="keyword-filter">
              商品搜索
              <TextInput
                aria-label="商品搜索"
                value={draft.keyword}
                maxLength={200}
                placeholder="商品名称 / 商品 ID / 运营"
                onChange={(e) => change('keyword', e.target.value)}
              />
            </label>
            <label>
              排序指标
              <Choice
                aria-label="商品排序指标"
                value={draft.sort_by}
                onChange={(e) => change('sort_by', e.target.value)}
              >
                {[
                  ['shop_name', '店铺名称'],
                  ['name', '商品名称'],
                  ['visitors', '访客数'],
                  ['views', '浏览量'],
                  ['sales', '成交金额'],
                  ['quantity', '成交件数'],
                  ['orders', '成交订单数'],
                  ['buyers', '成交买家数'],
                  ['conversion', '成交转化率'],
                  ['favorites', '收藏用户数'],
                  ['order_rate', '下单率'],
                  ['payment_rate', '成交率'],
                  ['traffic_loss', '流量损失指数'],
                ].map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </Choice>
            </label>
            <label>
              排序方式
              <Choice
                aria-label="商品排序方式"
                value={draft.sort_direction}
                onChange={(e) => change('sort_direction', e.target.value)}
              >
                <option value="desc">从高到低</option>
                <option value="asc">从低到高</option>
              </Choice>
            </label>
          </>
        )}
        {['packages', 'intercepts'].includes(kind) && (
          <label>
            快递公司
            <TextInput
              value={draft.carrier}
              placeholder="例如：申通"
              onChange={(e) => change('carrier', e.target.value)}
            />
          </label>
        )}
        {['aftersales', 'evidence', 'tickets', 'intercepts', 'packages', 'invoices', 'quality'].includes(kind) && (
          <label className="priority-filter">
            <TextInput
              type="checkbox"
              checked={draft.priority_only}
              onChange={(e) => change('priority_only', e.target.checked)}
            />
            仅看重点事项
          </label>
        )}
        <div className="filter-buttons">
          <NativeButton type="button" onClick={reset}>
            重置
          </NativeButton>
        </div>
      </form>
      {kind === 'activities' && (
        <nav className="activity-quick-filters" aria-label="活动快捷筛选">
          {['全部', '待处理', '审核中', '活动中', '已结束'].map((label) => (
            <NativeButton
              type="button"
              key={label}
              aria-pressed={draft.activity_group === (label === '全部' ? '' : label) && !draft.status}
              onClick={() =>
                setDraft((previous) => ({ ...previous, activity_group: label === '全部' ? '' : label, status: '' }))
              }
            >
              {label}
            </NativeButton>
          ))}
        </nav>
      )}
      <div className="workspace-controls">
        {kind === 'invoices' && <InvoiceImport onUpdated={() => refreshRef.current?.(false)} />}
        <DateCollection
          kind={kind}
          shops={shops}
          platform={platform}
          filters={draft}
          onCollected={(module, values) => onChangeKind?.(module, { ...defaults(kind), ...values })}
        />
        <SavedFilters
          scope={kind}
          draft={draft}
          pageSize={pageSize}
          onApply={(item) => {
            setDraft(cleanFilters(kind, { ...defaults(kind), ...item.draft }));
            if ([20, 50, 100].includes(item.pageSize)) setPageSize(item.pageSize);
            setPage(1);
          }}
        />
        <ExportButton
          kind={kind}
          filters={{ ...filters, platform }}
          disabled={!displayReadable || !data || JSON.stringify(draft) !== JSON.stringify(filters)}
        />
        <TablePreferences root={root} scope={kind} />
      </div>
      {operationNotes[kind] && (!taobaoOnly || !['aftersales', 'packages', 'invoices'].includes(kind)) && (
        <details className="context-note">
          <summary>统计说明</summary>
          <p>{operationNotes[kind]}</p>
        </details>
      )}
      {selectedPlatform && platformNotes}
      {draft.date_from && draft.date_to && draft.date_from > draft.date_to && (
        <p role="alert" className="error">
          开始日期不能晚于结束日期，请调整后查询。
        </p>
      )}
      {isTax && data?.difference_count > 0 && (
        <div className="difference-notice" role="status">
          {data.difference_count} 条主体月度记录需核对，已高亮；成功采集金额已自动更新，详情可查看异常原因与变更历史。
          {data.missing_count > 0 ? '存在平台未返回的账期，表格与合计暂沿用数据库金额，请核对。' : ''}
        </div>
      )}
      {data?.priority_count > 0 && (
        <div className="priority-summary">
          当前筛选有 <strong>{data.priority_count}</strong> 条重点事项，已高亮并优先显示。
        </div>
      )}
      {error && (
        <div className="notice" role="alert">
          {error}
        </div>
      )}
      <DataCoverage sources={sources} onRefresh={() => refreshRef.current?.(true)} />
      {isTax && <TaxSummary data={data} />}
      {isFinance && (
        <CollapsibleSection className="data-module">
          <h3>账期汇总</h3>
          <div className="metric-grid finance-summary">
            {[
              ['income', '已读取店铺收入合计'],
              ['outcome', '已读取店铺支出合计'],
              ['net', '已读取店铺净收支合计'],
            ].map(([key, label]) => (
              <div className="metric-card" key={key}>
                <span>{label}</span>
                <strong>{money(data?.finance_summary?.[key])}</strong>
              </div>
            ))}
          </div>
          <p className="data-note">
            合计覆盖所选月份 / 年份的全部已读取账期，不受分页影响。收入 {number(data?.finance_summary?.income_count)}{' '}
            笔，支出 {number(data?.finance_summary?.outcome_count)} 笔。期初与期末余额按每个账期单独展示。
          </p>
        </CollapsibleSection>
      )}
      {kind === 'goodsdata' ? (
        <GoodsData
          platform={selectedPlatform}
          shopRecords={shops}
          selectedShopId={filters.shop_id}
          onNavigate={onChangeKind}
          sort={tableSort}
          data={data}
          page={page}
          pageSize={pageSize}
          onPage={setPage}
          onPageSize={(value) => {
            setPageSize(value);
            setPage(1);
          }}
          onOpen={onOpen}
        />
      ) : kind === 'transactions' ? (
        <TransactionData
          platform={selectedPlatform}
          data={data}
          onCollectDetails={() => refreshRef.current?.(true, true)}
          loading={loading}
        />
      ) : (
        <CollapsibleSection className="data-module" aria-label="数据明细">
          <div className="table-caption">
            <strong>{data ? `共 ${data.total} 条` : '正在加载…'}</strong>
          </div>
          <div className="data-table-wrap">
            <SortableTable className="data-table" resetKey={JSON.stringify(filters) + page} serverSort={tableSort}>
              <thead>
                <tr>
                  {(isTax
                    ? taxHeaders(rows.some((r) => r.official_tax))
                    : isOperation
                      ? kind === 'aftersales' && shops.some((s) => s.platform === 'taobao')
                        ? [
                            '店铺',
                            '商品 / 订单',
                            '售后 / 包裹状态',
                            '退款 / 预估赔偿 / 原因',
                            '运单号',
                            '申请 / 发货时间',
                            '操作',
                          ]
                        : kind === 'orders' && draft.status === '退款/售后'
                          ? operationHeaders[kind].map((h) =>
                              h === '实收金额'
                                ? '退款金额'
                                : h === '下单时间'
                                  ? '申请时间'
                                  : h === '承诺发货时间'
                                    ? '处理期限'
                                    : h,
                            )
                          : operationHeaders[kind]
                      : kind === 'products'
                        ? ['店铺', '商品', '状态', '价格', '库存', '累计销量', '30 日销量', '创建时间', '操作']
                        : kind === 'tickets'
                          ? ['店铺', '工单 / 订单', '商品与问题', '状态', '处理进度', '创建时间', '截止时间', '操作']
                          : ['店铺', '商品 / 订单', '评分', '评价内容', '商家回复', '评价时间', '操作']
                  ).map((label) => (
                    <th key={label}>{label}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <Fragment key={`${row.shop_id}:${row.id}`}>
                    <tr
                      data-shop-id={row.shop_id}
                      data-shop-ids={row.shop_breakdown?.map((s) => s.shop_id).join(',')}
                      className={
                        row.db_changed
                          ? 'tax-difference-row'
                          : row.priority
                            ? `priority-row priority-${row.priority} ${['aftersales', 'evidence', 'tickets', 'intercepts', 'packages', 'invoices'].includes(kind) ? 'attention-row' : ''}`
                            : ''
                      }
                    >
                      <td>
                        {shopLabel(row)}
                        {row.focus_pending && (
                          <small className="focus-resolution-pending" role="status">
                            重点已解除 · 状态核对中
                          </small>
                        )}
                      </td>
                      {isTax ? (
                        <TaxCells row={row} official={rows.some((r) => r.official_tax)} />
                      ) : isOperation ? (
                        <OperationCells kind={kind} row={row} now={clockNow} />
                      ) : kind === 'products' ? (
                        <>
                          <td className="wide-cell">
                            <ProductInfo row={row}>
                              <strong>{row.name}</strong>
                              <small>ID：{row.goods_id}</small>
                              <small>
                                {row.category}
                                {row.code ? ` · ${row.code}` : ''}
                              </small>
                            </ProductInfo>
                          </td>
                          <td>
                            <StatusBadge value={row.status} />
                          </td>
                          <td className="nowrap">
                            {money(row.price_min)}
                            {row.price_max !== row.price_min ? ' ~ ' + money(row.price_max) : ''}
                          </td>
                          <td>{number(row.stock)}</td>
                          <td>{number(row.sales)}</td>
                          <td>{number(row.sales_30d)}</td>
                          <td className="date-cell">{row.created_at || '—'}</td>
                        </>
                      ) : kind === 'tickets' ? (
                        <>
                          <td>
                            <small>工单 {row.id}</small>
                            <small>订单 {row.order_number}</small>
                          </td>
                          <td className="wide-cell">
                            <ProductInfo row={row}>
                              <strong>{row.problem}</strong>
                              <small>{row.name}</small>
                            </ProductInfo>
                          </td>
                          <td>
                            <StatusBadge value={row.status} />
                            {row.priority > 0 && <small className="priority-label">{row.priority_reason}</small>}
                          </td>
                          <td>{row.progress || '—'}</td>
                          <td className="date-cell">{row.created_at || '—'}</td>
                          <td className="date-cell">{row.deadline || '—'}</td>
                        </>
                      ) : (
                        <>
                          <td className="wide-cell">
                            <ProductInfo row={row} />
                          </td>
                          <td>
                            <span className={`rating ${row.stars <= 3 ? 'low-rating' : ''}`}>
                              {row.stars ?? '—'} 星
                            </span>
                          </td>
                          <td className="wide-cell">
                            <div className="clamp-text" title={row.content || '无文字评价'}>
                              {row.content || '无文字评价'}
                            </div>
                            {row.has_images && <small>含图片</small>}
                            {row.has_video && <small>含视频</small>}
                          </td>
                          <td>
                            {row.replied ? (
                              <div className="clamp-text" title={row.reply || '已回复'}>
                                {row.reply || '已回复'}
                              </div>
                            ) : (
                              <StatusBadge value="未回复" />
                            )}
                          </td>
                          <td className="date-cell">{row.created_at || '—'}</td>
                        </>
                      )}
                      <td>
                        {isFinance && (
                          <NativeButton
                            className="table-action"
                            aria-expanded={!!expanded[`${row.shop_id}:${row.id}`]}
                            onClick={() =>
                              setExpanded((previous) => ({
                                ...previous,
                                [`${row.shop_id}:${row.id}`]: !previous[`${row.shop_id}:${row.id}`],
                              }))
                            }
                          >
                            {expanded[`${row.shop_id}:${row.id}`] ? '收起明细' : '展开明细'}
                          </NativeButton>
                        )}
                        <NativeButton className="table-action" onClick={() => setDetail(row)}>
                          详情
                        </NativeButton>
                      </td>
                    </tr>
                    {isFinance && expanded[`${row.shop_id}:${row.id}`] && (
                      <tr className="financial-expanded-row">
                        <td colSpan={10}>
                          <FinancialBreakdown row={row} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </SortableTable>
            {!rows.length && (
              <EmptyData
                loading={loading}
                error={error}
                sources={sources}
                onRetry={() => refreshRef.current?.()}
                onSync={() => refreshRef.current?.(true, true)}
              />
            )}
          </div>
          <div className="pagination">
            <span>
              第 {page} / {data?.page_count || 1} 页
            </span>
            <Choice
              aria-label="每页数量"
              value={pageSize}
              onChange={(e) => {
                setPageSize(Number(e.target.value));
                setPage(1);
              }}
            >
              {[20, 50, 100].map((size) => (
                <option key={size} value={size}>
                  {size} 条 / 页
                </option>
              ))}
            </Choice>
            <NativeButton disabled={page <= 1} onClick={() => setPage(page - 1)}>
              上一页
            </NativeButton>
            <NativeButton disabled={!data || page >= data.page_count} onClick={() => setPage(page + 1)}>
              下一页
            </NativeButton>
          </div>
        </CollapsibleSection>
      )}
      {!selectedPlatform && platformNotes}
      {isTax && (
        <TaxSubjects subjects={(data?.subjects || []).filter((s) => !filters.subject || s.id === filters.subject)} />
      )}
      {detail && (
        <AdminDialog>
          <section className="admin-dialog-content record-dialog" aria-label="数据详情">
            <div className="dialog-head">
              <h2>{isFinance ? (kind === 'reconciliation_day' ? '日汇总' : '月汇总') : titles[kind]}详情</h2>
              <NativeButton onClick={() => setDetail(null)}>关闭</NativeButton>
            </div>
            {!isMoney && kind !== 'ad_logs' && (detail.name || detail.goods_id) ? (
              <ProductInfo row={detail} showImage={kind !== 'invoices'}>
                <h3>{detail.name || '商品信息'}</h3>
                {detail.goods_id && <small>商品 {detail.goods_id}</small>}
              </ProductInfo>
            ) : (
              <h3>{detail.name}</h3>
            )}
            <p>{shopLabel(detail)}</p>
            {isTax && <TaxDetail row={detail} />}
            {kind === 'activities' && <ActivityDetail row={detail} />}
            {kind === 'invoices' && <InvoiceDetail row={detail} />}
            <dl hidden={isTax || kind === 'activities' || kind === 'invoices'}>
              {Object.entries({
                入账账期: isFinance ? detail.period : undefined,
                收入金额: detail.income == null ? undefined : money(detail.income),
                收入笔数: detail.income_count,
                支出金额: detail.outcome == null ? undefined : money(detail.outcome),
                支出笔数: detail.outcome_count,
                收支差额: detail.net == null ? undefined : money(detail.net),
                期初余额: detail.opening_balance == null ? undefined : money(detail.opening_balance),
                期末余额: detail.closing_balance == null ? undefined : money(detail.closing_balance),
                统计截至: detail.summary_through || undefined,
                操作人: detail.operator_name,
                操作模块: detail.operation_module,
                操作详情: kind === 'ad_logs' ? detail.content : undefined,
                记录编号: isFinance ? undefined : detail.id,
                商品编号: detail.goods_id,
                订单编号: detail.order_number,
                状态: detail.status,
                生效状态: detail.effect_status,
                距离揽收超时: kind === 'packages' ? pickupCountdown(detail, clockNow)?.text : undefined,
                揽收期限: detail.pickup_deadline?.replace('T', ' ').replace('+08:00', ''),
                评分: detail.stars ? `${detail.stars} 星` : undefined,
                评价内容: kind === 'reviews' ? detail.content || '无文字评价' : undefined,
                商家回复: kind === 'reviews' ? detail.reply || (detail.replied ? '已回复' : '未回复') : undefined,
                问题: detail.problem,
                处理进度: detail.progress,
                库存: detail.stock,
                累计销量: detail.sales,
                创建时间: isMoney ? undefined : detail.created_at,
                截止时间: detail.deadline,
                快递单号: detail.tracking_number,
                退货单号: detail.return_tracking_number,
                快递公司: detail.carrier,
                金额: detail.amount == null ? undefined : money(detail.amount),
                发票抬头: detail.invoice_title,
                发票号码: detail.invoice_number,
                举证交易金额: detail.order_amount == null ? undefined : money(detail.order_amount),
                售后类型: detail.aftersale_type,
                商家备注: detail.remark,
                最近物流时间: detail.logistics_time,
                拦截类型: detail.intercept_type,
                平台失败原因码: detail.failure_code,
                平台整改建议: detail.suggestion,
                统计日期: detail.stat_date,
                品质评价排名: detail.rating_rank == null ? undefined : detail.rating_rank + '%',
                品质退款率: detail.refund_rate == null ? undefined : detail.refund_rate + '%',
                品质求助平台率: detail.help_rate == null ? undefined : detail.help_rate + '%',
                文本情感分: detail.sentiment_score,
                负向反馈数: detail.negative_count,
                负向反馈率: detail.negative_rate == null ? undefined : detail.negative_rate + '%',
                差评数: detail.bad_count,
                差评率: detail.bad_rate == null ? undefined : detail.bad_rate + '%',
              })
                .filter(([, value]) => value !== undefined)
                .map(([label, value]) => (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd>{['状态', '生效状态'].includes(label) ? <StatusBadge value={value} /> : (value ?? '—')}</dd>
                  </div>
                ))}
            </dl>
            {isFinance && <FinancialBreakdown row={detail} />}{' '}
            {!isMoney && (
              <NativeButton
                className="primary"
                onClick={() => {
                  onOpen(detail.shop_id, kind);
                  setDetail(null);
                }}
              >
                打开该店铺后台页面
              </NativeButton>
            )}
          </section>
        </AdminDialog>
      )}
    </section>
  );
}
