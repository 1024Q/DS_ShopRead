'use client';
import { NativeButton, TextInput, Choice } from '../shared/AdminControls';
import { preferenceKey } from '../../lib/preference-scope.mjs';
import { PlatformFilterContext } from '../shared/PlatformFilter';
import UpdatedAgo from '../shared/UpdatedAgo';
import { shopLabel } from '../../lib/shop-identity.mjs';
import { pinnedColumn } from '../../lib/table-pinning.mjs';
import { useContext, useEffect, useRef, useState } from 'react';

export const moduleNames = {
  orders: '订单查询',
  ad_logs: '操作记录',
  ad_products: '商品推广',
  ad_reports: '推广报表',
  overview: '实时概览',
  transactions: '交易数据',
  tax: '报税信息',
  reconciliation_day: '对账日汇总',
  reconciliation_month: '对账月汇总',
  packages: '包裹中心',
  invoices: '订单开票',
  brands: '品牌黑标权益',
  activities: '活动报名记录',
  products: '商品列表',
  goodsdata: '商品数据',
  reviews: '评价管理',
  aftersales: '售后工作台',
  evidence: '商家举证',
  tickets: '工单列表',
  intercepts: '拦截快递',
  quality: '品质诊断',
  navigator: '商品领航员',
};
export const beijing = (value) =>
  value
    ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('zh-CN', {
        timeZone: 'Asia/Shanghai',
        hour12: false,
      })
    : '未提供';
export function queryString(values) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(values))
    if (v !== '' && v != null && (!Array.isArray(v) || v.length)) p.set(k, Array.isArray(v) ? v.join(',') : v);
  return p;
}

export function sourceScope(s) {
  if (s.kind === 'overview') return '平台实时经营指标';
  if (s.kind === 'products') return '商品列表（不限日期）';
  if (s.kind === 'activities') return '近期报名记录（不含归档）';
  if (['navigator', 'brands'].includes(s.kind)) return '平台当前数据';
  return (
    s.scope?.period ||
    [s.scope?.date_from, s.scope?.date_to].filter(Boolean).join(' 至 ') ||
    { realtime: '今日实时', yesterday: '昨日', week: '近 7 日', month: '近 30 日' }[s.scope?.trade_range] ||
    '默认范围'
  );
}

export function SourceStatus({ sources = [], onOpen }) {
  const missing = sources.filter((s) => !s.updated_at),
    partial = sources.filter((s) => s.updated_at && !s.complete),
    errors = sources.filter((s) => s.error);
  const labels = {
    queued: '排队中',
    running: '读取中',
    waiting_login: '等待登录',
    error: '读取失败',
    missing: '尚未读取',
    partial: '部分数据',
    empty: '平台无数据',
    ready: '读取完成',
  };
  return (
    <>
      <div className="sync-summary">
        <strong>
          {sources.filter((s) => s.updated_at).length} / {sources.length} 个数据源已有记录
        </strong>
        <span>失败时保留上次数据 · 当前查看的模块优先同步</span>
      </div>
      {(missing.length > 0 || partial.length > 0 || errors.length > 0) && (
        <div className="coverage-notice" role="status">
          {missing.length > 0 && (
            <p>缺失数据：{missing.map((s) => shopLabel(s) + (s.kind ? ' · ' + moduleNames[s.kind] : '')).join('、')}</p>
          )}
          {partial.length > 0 && (
            <p>部分读取：{partial.map((s) => shopLabel(s) + (s.kind ? ' · ' + moduleNames[s.kind] : '')).join('、')}</p>
          )}
          {errors.length > 0 && <p>{errors.length} 个数据源读取失败，请查看下方原因；汇总与导出仅包含已读取记录。</p>}
        </div>
      )}
      <div className="source-list">
        {sources.map((s) => (
          <div key={`${s.shop_id}:${s.kind || ''}`} className={`source-chip ${s.error ? 'source-error' : ''}`}>
            <strong>
              {shopLabel(s)}
              {s.kind ? ' · ' + moduleNames[s.kind] : ''}
            </strong>
            <span>
              {s.recovery_status ||
                labels[s.data_status] ||
                (s.syncing ? '读取中' : s.error ? '读取失败' : s.updated_at ? '已读取' : '尚未读取')}
              {s.stale && s.updated_at ? ' · 上次数据' : ''}
            </span>
            {s.syncing && (
              <>
                <small>
                  {s.sync_phase === 'queued'
                    ? '等待可用店铺浏览器'
                    : `${s.sync_stage || '读取中'} · 已读取 ${s.sync_loaded || 0}${s.sync_total != null ? ' / ' + s.sync_total : ''} · ${s.elapsed_seconds ?? 0} 秒`}
                </small>
                {s.sync_total > 0 && <progress max={s.sync_total} value={s.sync_loaded || 0} />}
              </>
            )}
            {s.error && <small className="error">{s.error}</small>}
            <small>
              系统读取：
              <UpdatedAgo value={s.updated_at} />
            </small>
            <small>平台更新：{s.source_time || s.latest_summary_day || '未提供'}</small>
            {s.scope && (
              <small>
                查询范围：{sourceScope(s)}
                {s.scope.status ? ' · ' + s.scope.status : ''}
              </small>
            )}
            {s.updated_at && (
              <small>
                {s.kind === 'overview'
                  ? '经营指标快照'
                  : `${s.loaded_count} 条明细 / 平台 ${s.source_total ?? '未知'} ${s.source_unit || '条'}`}
                {s.duration_seconds != null ? ` · 上次耗时 ${s.duration_seconds} 秒` : ''}
              </small>
            )}
            {onOpen && (
              <NativeButton className="table-action" onClick={() => onOpen(s.shop_id, s.kind)}>
                查看后台
              </NativeButton>
            )}
          </div>
        ))}
      </div>
    </>
  );
}

export function ToolPopover({ label, className = '', children }) {
  const ref = useRef(null);
  useEffect(() => {
    const outside = (e) => {
      if (ref.current?.open && !ref.current.contains(e.target)) ref.current.open = false;
    };
    const escape = (e) => {
      if (e.key === 'Escape' && ref.current?.open) {
        ref.current.open = false;
        ref.current.querySelector('summary')?.focus();
      }
    };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape);
    };
  }, []);
  return (
    <details className={`tool-popover ${className}`} ref={ref}>
      <summary>{label}</summary>
      <div className="tool-popover-panel">{children}</div>
    </details>
  );
}

export function SavedFilters({ scope, draft, pageSize, onApply }) {
  const [items, setItems] = useState([]),
    [name, setName] = useState(''),
    [selected, setSelected] = useState(''),
    [error, setError] = useState('');
  const platformScope = useContext(PlatformFilterContext);
  const key = preferenceKey('pdd-manager:presets:' + scope);
  useEffect(() => {
    try {
      const v = JSON.parse(localStorage.getItem(key) || '[]');
      if (Array.isArray(v)) setItems(v);
    } catch {}
  }, [key]);
  const save = (next) => {
    try {
      localStorage.setItem(key, JSON.stringify(next));
      setItems(next);
      setError('');
    } catch {
      setError('浏览器存储不可用，筛选方案未保存');
    }
  };
  return (
    <ToolPopover label="常用筛选 ▾" className="saved-filters">
      <div className="workspace-toolbar">
        <label>
          使用方案
          <Choice
            aria-label="常用筛选"
            value={selected}
            onChange={(e) => {
              setSelected(e.target.value);
              const item = items.find((i) => i.name === e.target.value);
              if (item) {
                if (item.platform !== undefined) platformScope?.onChange(item.platform);
                onApply(item);
              }
            }}
          >
            <option value="">选择已保存方案</option>
            {items.map((i) => (
              <option key={i.name}>{i.name}</option>
            ))}
          </Choice>
        </label>
        <TextInput
          aria-label="筛选方案名称"
          placeholder="方案名称，例如：未回复差评"
          maxLength={40}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <NativeButton
          type="button"
          disabled={!name.trim()}
          onClick={() => {
            const title = name.trim();
            save(
              [
                ...items.filter((i) => i.name !== title),
                { name: title, draft, pageSize, platform: platformScope?.platform || '' },
              ].slice(-30),
            );
            setSelected(title);
            setName('');
          }}
        >
          保存当前筛选
        </NativeButton>
        <NativeButton
          type="button"
          disabled={!selected}
          onClick={() => {
            save(items.filter((i) => i.name !== selected));
            setSelected('');
          }}
        >
          删除方案
        </NativeButton>
        {error && <span className="error">{error}</span>}
      </div>
    </ToolPopover>
  );
}

export function ExportButton({ kind, filters, disabled }) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [progress, setProgress] = useState('');
  async function download() {
    setBusy(true);
    setError('');
    setProgress('正在提交导出…');
    try {
      const p = queryString({ ...filters, export: true, background: true });
      let r = await fetch(`/api/data/${kind}?${p}`);
      if (!r.ok) {
        const d = await r.json();
        throw Error(typeof d.detail === 'string' ? d.detail : '导出失败');
      }
      if ((r.headers.get('content-type') || '').includes('json')) {
        const job = await r.json();
        if (!job.id) throw Error('导出任务创建失败');
        setProgress('后台生成中…');
        for (;;) {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          r = await fetch('/api/exports/' + job.id);
          if (!r.ok) throw Error('无法读取导出进度');
          const state = await r.json();
          setProgress(state.status === 'queued' ? '等待导出名额' : `已写入 ${state.rows_written || 0} 行`);
          if (state.status === 'error') throw Error(state.error);
          if (state.status === 'complete') break;
        }
        r = await fetch('/api/exports/' + job.id + '/download');
        if (!r.ok) throw Error('导出文件下载失败');
      }
      const url = URL.createObjectURL(await r.blob());
      const a = document.createElement('a');
      a.href = url;
      a.download = `${moduleNames[kind] || kind}-筛选结果.xlsx`;
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="export-action">
      <NativeButton disabled={disabled || busy} onClick={download}>
        {busy ? progress || '正在提交导出…' : '导出筛选结果 Excel'}
      </NativeButton>
      {error && (
        <span className="error" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}

export function TablePreferences({ root, scope }) {
  const [tables, setTables] = useState([]),
    [selected, setSelected] = useState(''),
    [prefs, setPrefs] = useState({}),
    [ready, setReady] = useState(false);
  const live = useRef({}),
    saving = useRef(null),
    scanRef = useRef(null),
    key = 'pdd-manager:columns:' + scope;
  useEffect(() => {
    try {
      live.current = JSON.parse(localStorage.getItem(key) || '{}');
    } catch {
      live.current = {};
    }
    setPrefs(live.current);
    setReady(true);
  }, [key]);
  const persist = (next) => {
    live.current = next;
    setPrefs({ ...next });
    try {
      localStorage.setItem(key, JSON.stringify(next));
    } catch {}
  };
  saving.current = persist;
  useEffect(() => {
    if (!ready || !root.current) return;
    const host = root.current,
      cleanups = [];
    function scan() {
      const found = [];
      for (const table of host.querySelectorAll('table.data-table')) {
        if (table.closest('.overlay') || table.parentElement.closest('table')) continue;
        const heads = [...(table.tHead?.rows[0]?.cells || [])];
        if (!heads.length) continue;
        const names = heads.map((h) => h.textContent.trim()),
          id = names.join('|');
        if (!live.current[id] && ['goodsdata', 'ad_products', 'ad_reports'].includes(scope) && names.length > 12) {
          live.current[id] = Object.fromEntries(
            names.map((name, index) => [
              index,
              {
                hidden:
                  index > 0 &&
                  !/店铺|运营|商品|计划|场景|状态|成交金额|成交订单|访客数|成交花费|交易额|投产|操作/.test(name),
              },
            ]),
          );
          setPrefs({ ...live.current });
        }
        const config = live.current[id] || {};
        found.push({ id, names });
        table.classList.add('custom-table');
        const pins = names.map(pinnedColumn);
        heads.forEach((head, index) => {
          const column = config[index] || {},
            hidden = !!column.hidden,
            width = column.width;
          const col = table.querySelectorAll(':scope > colgroup > col')[index];
          if (col) {
            col.style.width = (width || Number(col.dataset.baseWidth)) + 'px';
            col.style.display = hidden ? 'none' : '';
          }
          for (const row of table.rows) {
            const cell = row.cells[index];
            if (!cell || row.cells.length !== heads.length) continue;
            cell.hidden = hidden;
            cell.style.width = width ? width + 'px' : '';
            cell.style.minWidth = width ? width + 'px' : '';
            cell.style.maxWidth = width ? width + 'px' : '';
            // Ant Design hover renders cells again; their React props own pin classes.
            if (!table.classList.contains('ant-business-grid')) {
              cell.classList.toggle('pinned-column', pins[index]);
              cell.classList.toggle('action-column', index === heads.length - 1 && names[index] === '操作');
            }
          }
          if (head.querySelector('.column-resizer')) return;
          const handle = document.createElement('span');
          handle.className = 'column-resizer';
          handle.setAttribute('role', 'separator');
          handle.setAttribute('aria-label', '调整 ' + names[index] + ' 列宽');
          handle.setAttribute('aria-orientation', 'vertical');
          handle.tabIndex = 0;
          const resize = (value) => {
            const all = live.current,
              current = all[id] || {};
            saving.current({
              ...all,
              [id]: { ...current, [index]: { ...current[index], width: Math.max(90, Math.min(720, value)) } },
            });
            scan();
          };
          handle.onkeydown = (e) => {
            if (['ArrowLeft', 'ArrowRight'].includes(e.key)) {
              e.preventDefault();
              resize(head.getBoundingClientRect().width + (e.key === 'ArrowRight' ? 20 : -20));
            }
          };
          handle.onpointerdown = (e) => {
            e.preventDefault();
            e.stopPropagation();
            const start = e.clientX,
              width = head.getBoundingClientRect().width;
            let target = width,
              frame = 0;
            const cells = Array.from(table.rows)
              .filter((r) => r.cells.length === heads.length)
              .map((r) => r.cells[index]);
            handle.setPointerCapture(e.pointerId);
            handle.onpointermove = (move) => {
              target = Math.max(90, Math.min(720, width + move.clientX - start));
              cancelAnimationFrame(frame);
              frame = requestAnimationFrame(() => {
                for (const cell of cells) {
                  cell.style.width = cell.style.minWidth = cell.style.maxWidth = target + 'px';
                }
                if (col) col.style.width = target + 'px';
              });
            };
            handle.onpointerup = handle.onpointercancel = () => {
              cancelAnimationFrame(frame);
              handle.onpointermove = null;
              resize(target);
            };
          };
          head.append(handle);
          cleanups.push(() => handle.remove());
        });
        // Batch layout reads after width writes, then apply all sticky offsets together.
        const measured = heads.map((head) => head.getBoundingClientRect().width);
        let left = 0;
        heads.forEach((head, index) => {
          for (const row of table.rows) {
            if (row.cells.length === heads.length) row.cells[index].style.left = pins[index] ? left + 'px' : '';
          }
          if (pins[index] && !config[index]?.hidden) left += measured[index];
        });
      }
      const unique = [...new Map(found.map((t) => [t.id, t])).values()];
      setTables((old) => (JSON.stringify(old) === JSON.stringify(unique) ? old : unique));
    }
    scanRef.current = scan;
    scan();
    let frame;
    const observer = new MutationObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(scan);
    });
    observer.observe(host, { childList: true, subtree: true });
    window.addEventListener('resize', scan);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
      window.removeEventListener('resize', scan);
      cleanups.forEach((fn) => fn());
    };
  }, [root, ready, scope]);
  useEffect(() => {
    scanRef.current?.();
  }, [prefs]);
  const table = tables.find((t) => t.id === selected) || tables[0];
  const preset = (mode) => {
    if (!table) return;
    const keep = {
      core: /店铺|商品$|状态|成交金额|成交订单|访客数|总花费|投产|操作/,
      conversion: /店铺|商品$|状态|曝光|点击|转化|加购|收藏|买家|操作/,
      refund: /店铺|商品$|状态|退款|净|结算|操作/,
    }[mode];
    persist({
      ...prefs,
      [table.id]: Object.fromEntries(
        table.names.map((name, index) => [
          index,
          { ...prefs[table.id]?.[index], hidden: mode !== 'all' && index > 0 && !keep.test(name) },
        ]),
      ),
    });
  };
  if (!table) return null;
  return (
    <ToolPopover label="⚙ 表格设置" className="column-settings">
      <p className="data-note">拖动表头分隔线调整列宽，也可选择显示的列。</p>
      <div className="workspace-toolbar">
        <label>
          选择表格
          <Choice value={table.id} onChange={(e) => setSelected(e.target.value)}>
            {tables.map((t, i) => (
              <option key={t.id} value={t.id}>
                表格 {i + 1} · {t.names.slice(0, 3).join(' / ')}
              </option>
            ))}
          </Choice>
        </label>
        <NativeButton
          onClick={() => {
            const next = { ...prefs };
            delete next[table.id];
            persist(next);
          }}
        >
          恢复默认列
        </NativeButton>
      </div>
      <div className="column-presets">
        {[
          ['core', '核心指标'],
          ['conversion', '流量转化'],
          ['refund', '退款与净额'],
          ['all', '全部字段'],
        ].map(([id, label]) => (
          <NativeButton key={id} onClick={() => preset(id)}>
            {label}
          </NativeButton>
        ))}
      </div>
      <div className="column-checkboxes">
        {table.names.map((name, index) => (
          <label key={index}>
            <TextInput
              type="checkbox"
              checked={!prefs[table.id]?.[index]?.hidden}
              disabled={index === 0}
              onChange={(e) =>
                persist({
                  ...prefs,
                  [table.id]: {
                    ...prefs[table.id],
                    [index]: { ...prefs[table.id]?.[index], hidden: !e.target.checked },
                  },
                })
              }
            />
            {name}
          </label>
        ))}
      </div>
    </ToolPopover>
  );
}
