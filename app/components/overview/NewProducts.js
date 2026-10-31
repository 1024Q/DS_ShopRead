'use client';
import { ActionButton, Choice } from '../shared/AdminControls';
import { inScope } from '../../lib/filter-scope.mjs';
import CollapsibleSection from '../shared/CollapsibleSection';
import OperatorArrivals from './OperatorArrivals';
import { shopLabel } from '../../lib/shop-identity.mjs';
import { useEffect, useState } from 'react';
import { useWorkspace } from '../../providers/WorkspaceContext';
import useReportData from '../../hooks/useReportData';
import { canReadScope, canReadKind, platforms } from '../../lib/platforms.mjs';
export default function NewProducts({ shops, shopId, platform, onNavigate, suppliedData }) {
  const readOnly = suppliedData !== undefined;
  const [expanded, setExpanded] = useState(true);
  const { businessDay: today } = useWorkspace();
  useEffect(() => {
    setDay('');
    setYear('');
  }, [today]);
  const [selectedDay, setDay] = useState(''),
    [selectedYear, setYear] = useState(''),
    [open, setOpen] = useState(false);
  const day = selectedDay || today,
    year = selectedYear || Number(today.slice(0, 4));
  const report = useReportData(
    'products',
    { shop_id: shopId, platform, dashboard: true, date_from: day, date_to: day, new_products_year: year },
    !readOnly && expanded && canReadScope(shops, shopId, 'products'),
  );
  const data = readOnly ? suppliedData : report.data,
    error = readOnly ? suppliedData?.error : report.error;
  const details = (from, to = from, id = shopId) =>
    onNavigate('products', {
      platform,
      shop_id: id,
      date_from: from,
      date_to: to,
      status: '',
      keyword: '',
      min_price: '',
      max_price: '',
      low_stock: '',
    });
  const shopRows = shops
    .filter((s) => inScope(shopId, s.id))
    .map((s) => ({
      shop_id: s.id,
      shop_name: s.name || `店铺 ${s.id}`,
      shop_label: shopLabel(s),
      pending: !canReadKind(s, 'products'),
      ...data?.shops?.find((r) => r.shop_id === s.id),
    }));
  const monthly = data?.monthly || [],
    max = Math.max(1, ...monthly.map((m) => m.count || 0)),
    gap = data?.total != null && data?.previous_total != null ? data.total - data.previous_total : null;
  return (
    <CollapsibleSection className="new-products-panel ov-card" onExpandedChange={setExpanded}>
      {data?._preview_cached_at && <p className="data-note">上次上新统计 · 正在核对数据库</p>}
      <div className="ov-card-head">
        <div>
          <h3>
            上新统计 <small>商品首次创建</small>
          </h3>
        </div>
      </div>
      <div className="arrival-comparison">
        {[
          [day, day === today ? '今日上新' : '所选日期', data?.total],
          [data?.previous_date, day === today ? '昨日上新' : '前一日', data?.previous_total],
        ].map(([date, label, total]) => (
          <ActionButton key={label} disabled={!date} onClick={() => details(date)}>
            <span>
              {label}
              <small>{date}</small>
            </span>
            <strong>
              {total ?? '—'}
              <small>件 ↗</small>
            </strong>
          </ActionButton>
        ))}
        <div className="arrival-delta">
          <span>比前一日</span>
          <strong>
            {gap == null ? '—' : `${gap > 0 ? '+' : ''}${gap}`}
            <small> 件</small>
          </strong>
          <small>点击上新数查看商品明细</small>
        </div>
      </div>
      <OperatorArrivals
        people={data?.operators}
        day={day}
        today={today}
        year={year}
        onDetails={(ids) => details(day, day, ids)}
      />
      <div className="arrival-month-heading">
        <h4>每月上新</h4>
        <Choice
          aria-label="上新统计年份"
          disabled={readOnly}
          title={readOnly ? '免登录概览展示当前年份' : undefined}
          value={year}
          onChange={(e) => setYear(Number(e.target.value))}
        >
          {Array.from({ length: 10 }, (_, i) => Number(today.slice(0, 4)) - i).map((y) => (
            <option key={y} value={y}>
              {y} 年
            </option>
          ))}
        </Choice>
      </div>
      <div className="arrival-months">
        {monthly.map((m) => {
          const future = m.period > today.slice(0, 7),
            start = m.period + '-01',
            end = new Date(Date.UTC(year, Number(m.period.slice(5)), 0)).toISOString().slice(0, 10);
          return (
            <ActionButton
              key={m.period}
              disabled={future || m.count == null}
              onClick={() => details(start, end)}
              aria-label={`${m.period} 上新 ${future ? '尚未到来' : (m.count ?? '未读取')} 件`}
            >
              <strong>{future ? '—' : (m.count ?? '—')}</strong>
              <div className="month-bar-track">
                <i style={{ height: future ? '0' : `${((m.count || 0) / max) * 100}%` }} />
              </div>
              <span>{Number(m.period.slice(5))} 月</span>
            </ActionButton>
          );
        })}
      </div>
      <div className="arrival-note">
        <p>
          已读取 {data?.ready || 0} / {shopRows.length} 家。按已保存商品快照统计，不含快照缺失的历史商品。
          {!data?.complete ? '部分店铺或创建日期未读取完整。' : ''}
          {error || ''}
        </p>
        <ActionButton aria-expanded={open} onClick={() => setOpen(!open)}>
          各店铺上新 {open ? '⌃' : '⌄'}
        </ActionButton>
      </div>
      {open && (
        <div className="new-products-shops">
          {shopRows.map((s) => (
            <ActionButton key={s.shop_id} onClick={() => details(day, day, String(s.shop_id))}>
              <span>
                {shopLabel(s)}
                <small>
                  {shops.find((v) => v.id === s.shop_id)?.operator || '未分配运营'} ·{' '}
                  {s.pending ? '业务数据待接入' : s.updated_at ? '已保存快照' : '尚未读取'}
                </small>
              </span>
              <strong>
                {s.count ?? '—'}
                <small>前一日 {s.previous ?? '—'} 件</small>
              </strong>
            </ActionButton>
          ))}
        </div>
      )}
    </CollapsibleSection>
  );
}
