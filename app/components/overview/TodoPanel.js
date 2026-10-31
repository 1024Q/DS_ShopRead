'use client';
import { NativeButton, TextInput, Choice } from '../shared/AdminControls';
import { preferenceKey } from '../../lib/preference-scope.mjs';
import useDebounced from '../../hooks/useDebounced';
import useQueryLocation from '../../hooks/useQueryLocation';
import MultiSelect from '../shared/MultiSelect';
import { validSelection } from '../../lib/filter-scope.mjs';
import { useEffect, useRef, useState } from 'react';
import NotificationHistory from '../settings/NotificationHistory';
import AttentionPanel from './AttentionPanel';
import RefundActions from './RefundActions';
import PlatformFilter from '../shared/PlatformFilter';
import { shopLabel } from '../../lib/shop-identity.mjs';
import { TablePreferences, SavedFilters, moduleNames } from '../shell/WorkspaceTools';
const kinds = ['aftersales', 'evidence', 'tickets', 'intercepts', 'packages', 'invoices'];
export default function TodoPanel({ platform, shops, onNavigate, onOpen, initialFilters }) {
  const [tab, setTab] = useState(initialFilters?.tab === 'changes' ? 'changes' : 'todos');
  const [filters, setFilters] = useState({ shop_id: '', kind: '', keyword: '' }),
    [ready, setReady] = useState(false);
  const root = useRef(null);
  const keyword = useDebounced(filters.keyword);
  const queryFilters = { ...filters, keyword };
  useQueryLocation('todos', { ...queryFilters, platform, tab }, ready);
  useEffect(() => {
    try {
      const value = JSON.parse(localStorage.getItem(preferenceKey('pdd-manager:todos')) || 'null');
      const next = { ...value, ...initialFilters };
      setFilters((f) => ({
        ...f,
        ...next,
        kind: kinds.includes(next.kind) ? next.kind : '',
        shop_id: validSelection(next.shop_id, shops),
      }));
    } catch {}
    setReady(true);
  }, []);
  useEffect(() => {
    if (ready)
      try {
        localStorage.setItem(preferenceKey('pdd-manager:todos'), JSON.stringify(filters));
      } catch {}
  }, [filters, ready]);
  const previousPlatform = useRef(platform);
  useEffect(() => {
    if (previousPlatform.current === platform) return;
    previousPlatform.current = platform;
    const next = { ...filters, shop_id: validSelection(filters.shop_id, shops) };
    setFilters(next);
  }, [platform]);
  const change = (k, v) => setFilters((f) => ({ ...f, [k]: v }));
  return (
    <section className="business-panel" ref={root}>
      <div className="business-heading">
        <div>
          <h2>重点待办中心</h2>
        </div>
        <RefundActions shops={shops} />
      </div>
      <div className="ov-segments">
        <NativeButton aria-pressed={tab === 'todos'} onClick={() => setTab('todos')}>
          当前待办
        </NativeButton>
        <NativeButton aria-pressed={tab === 'changes'} onClick={() => setTab('changes')}>
          变化记录
        </NativeButton>
      </div>
      <div className="filter-panel">
        <PlatformFilter />
        <label>
          店铺
          <MultiSelect
            label="店铺筛选"
            value={filters.shop_id}
            onChange={(value) => change('shop_id', value)}
            options={shops.map((s) => ({ value: s.id, label: shopLabel(s) }))}
            placeholder="全部店铺"
          />
        </label>
        <label>
          事项类型
          <Choice value={filters.kind} onChange={(e) => change('kind', e.target.value)}>
            <option value="">全部事项</option>
            {kinds.map((k) => (
              <option key={k} value={k}>
                {moduleNames[k]}
              </option>
            ))}
          </Choice>
        </label>
        <label>
          搜索
          <TextInput
            value={filters.keyword}
            onChange={(e) => change('keyword', e.target.value)}
            placeholder="订单 / 工单 / 商品 / 快递单号 / 运营"
          />
        </label>
      </div>
      <div className="workspace-controls">
        <SavedFilters
          scope="todos"
          draft={filters}
          onApply={(i) =>
            setFilters((f) => ({ ...f, ...i.draft, kind: kinds.includes(i.draft.kind) ? i.draft.kind : '' }))
          }
        />
        <TablePreferences root={root} scope="todos" />
      </div>
      {tab === 'changes' ? (
        <NotificationHistory onNavigate={onNavigate} platform={platform} filters={queryFilters} />
      ) : (
        ready && (
          <AttentionPanel
            platform={platform}
            shopId={filters.shop_id}
            kind={filters.kind}
            keyword={keyword}
            onOpen={onOpen}
            onNavigate={onNavigate}
          />
        )
      )}
    </section>
  );
}
