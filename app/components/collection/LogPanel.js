'use client';
import { NativeButton, TextInput, Choice } from '../shared/AdminControls';
import { moduleNames } from '../shell/WorkspaceTools';
import MultiSelect from '../shared/MultiSelect';
import usePersistentSelection from '../../hooks/usePersistentSelection';
import { shopLabel } from '../../lib/shop-identity.mjs';
import SortableTable from '../shared/SortableTable';
import { useEffect, useState } from 'react';
const levels = { info: '信息', warning: '提醒', error: '错误' };
const modules = {
  ...moduleNames,
  scheduler: '采集调度',
  collection: '采集',
  system: '系统',
  ad_logs: '操作记录',
  ad_products: '商品推广',
  ad_reports: '推广报表',
  shops: '店铺管理',
  activities: '营销活动',
  goodsdata: '商品数据',
  transactions: '交易数据',
  login: '登录',
  api: '服务接口',
  frontend: '前端页面',
  tax: '报税信息',
  overview: '经营概览',
  reconciliation_day: '日汇总',
  reconciliation_month: '月汇总',
  products: '商品',
  reviews: '评价',
  tickets: '工单',
  aftersales: '售后',
  packages: '包裹',
  invoices: '开票',
  evidence: '举证',
  intercepts: '快递拦截',
  navigator: '商品领航员',
  quality: '品质诊断',
  brands: '黑标权益',
};
export default function LogPanel({ shops = [], compact = false, onOpen }) {
  const [rows, setRows] = useState([]),
    [error, setError] = useState(''),
    [level, setLevel] = useState(''),
    [shop, setShop] = usePersistentSelection('logs-shops'),
    [keyword, setKeyword] = useState('');
  const [module, setModule] = useState(''),
    [from, setFrom] = useState(''),
    [to, setTo] = useState(''),
    [cursor, setCursor] = useState(null),
    [next, setNext] = useState(null),
    [history, setHistory] = useState([]),
    [loading, setLoading] = useState(true);
  useEffect(() => {
    setCursor(null);
    setHistory([]);
  }, [level, shop, keyword, module, from, to]);
  useEffect(() => {
    if (from && to && from > to) return;
    let active = true,
      busy = false,
      watermark = 0;
    setLoading(true);
    const controller = new AbortController();
    const load = async () => {
      if (busy || document.hidden) return;
      busy = true;
      try {
        const params = new URLSearchParams({
          limit: compact ? '8' : '100',
          level,
          keyword,
          module,
          date_from: from,
          date_to: to,
          ...(cursor || {}),
          ...(!cursor && watermark ? { after: String(watermark) } : {}),
        });
        if (shop) params.set('shop_id', shop);
        const response = await fetch('/api/logs?' + params, { cache: 'no-store', signal: controller.signal });
        if (!response.ok) throw Error('日志服务暂不可用');
        const data = await response.json();
        if (active) {
          const incremental = watermark;
          setRows((old) => {
            const merged =
              incremental && !cursor
                ? [...new Map([...old, ...data.rows].map((r) => [r.id, r])).values()]
                    .sort((a, b) => b.last_seen - a.last_seen || b.id - a.id)
                    .slice(0, compact ? 8 : 100)
                : data.rows;
            if (merged.length >= 100) {
              const last = merged.at(-1);
              setNext({ before: last.last_seen, before_id: last.id });
            }
            return JSON.stringify(old) === JSON.stringify(merged) ? old : merged;
          });
          if (!watermark) setNext(data.next);
          watermark = Math.max(watermark, ...data.rows.map((r) => r.last_seen));
          setLoading(false);
          setError('');
        }
      } catch (e) {
        if (active && e.name !== 'AbortError') setError('日志连接中断，正在重试…');
      } finally {
        busy = false;
        if (active) setLoading(false);
      }
    };
    document.addEventListener('visibilitychange', load);
    const initial = setTimeout(load, 300),
      timer = cursor ? null : setInterval(load, 5000);
    return () => {
      active = false;
      clearTimeout(initial);
      clearInterval(timer);
      controller.abort();
      document.removeEventListener('visibilitychange', load);
    };
  }, [compact, level, shop, keyword, module, from, to, cursor]);
  const list = (
    <>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <div className="data-table-wrap">
        <SortableTable className="data-table log-table">
          <thead>
            <tr>
              {['时间', '级别', '店铺', '模块', '日志内容'].map((label) => (
                <th key={label}>{label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr data-shop-id={row.shop_id} key={row.id} className={'log-' + row.level}>
                <td className="date-cell">
                  {new Date(row.last_seen * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}
                </td>
                <td>{levels[row.level]}</td>
                <td>{row.shop_id != null ? shopLabel(row) : '系统'}</td>
                <td>{modules[row.module] || row.module}</td>
                <td>
                  <details>
                    <summary title={row.message}>
                      {row.message?.slice(0, 110)}
                      {row.message?.length > 110 ? '…' : ''}
                    </summary>
                    <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{row.message}</p>
                    <NativeButton onClick={() => navigator.clipboard.writeText(row.message).catch(() => {})}>
                      复制日志
                    </NativeButton>
                    {row.shop_id && (
                      <NativeButton
                        onClick={() =>
                          window.dispatchEvent(new CustomEvent('workspace-navigate', { detail: { view: 'shops' } }))
                        }
                      >
                        店铺管理 ↗
                      </NativeButton>
                    )}
                  </details>
                  {row.occurrences > 1 && <small>同类事件累计 {row.occurrences} 次</small>}
                </td>
              </tr>
            ))}
          </tbody>
        </SortableTable>
        {!rows.length && <p className="table-empty">{loading ? '正在读取日志…' : '暂无匹配日志'}</p>}
      </div>
    </>
  );
  if (compact)
    return (
      <details className="live-log-strip">
        <summary>
          实时运行反馈 · {error || rows[0]?.message || '等待日志'}
          <small>每 5 秒增量更新</small>
        </summary>
        {list}
        <NativeButton onClick={onOpen}>查看全部日志</NativeButton>
      </details>
    );
  return (
    <section className="business-panel logs-panel">
      <div className="business-heading">
        <div>
          <h2>运行与错误日志</h2>
        </div>
      </div>
      <div className="filter-panel">
        <label>
          日志级别
          <Choice aria-label="日志级别" value={level} onChange={(e) => setLevel(e.target.value)}>
            <option value="">全部级别</option>
            {Object.entries(levels).map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </Choice>
        </label>
        <label>
          店铺
          <MultiSelect
            label="日志店铺"
            value={shop}
            onChange={(value) => setShop(value)}
            options={shops.map((s) => ({ value: s.id, label: shopLabel(s) }))}
            placeholder="全部店铺"
          />
        </label>
        <label>
          关键词
          <TextInput
            aria-label="日志关键词"
            value={keyword}
            maxLength={100}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="错误原因 / 模块 / 店铺 / 运营"
          />
        </label>
        <label>
          模块
          <Choice value={module} onChange={(e) => setModule(e.target.value)}>
            <option value="">全部模块</option>
            {Object.entries(modules).map(([key, label]) => (
              <option value={key} key={key}>
                {label}
              </option>
            ))}
          </Choice>
        </label>
        <label>
          开始日期
          <TextInput type="date" value={from} max={to || undefined} onChange={(e) => setFrom(e.target.value)} />
        </label>
        <label>
          结束日期
          <TextInput type="date" value={to} min={from || undefined} onChange={(e) => setTo(e.target.value)} />
        </label>
      </div>
      {from && to && from > to && <p className="error">开始日期不能晚于结束日期</p>}
      {list}
      <div className="pagination">
        <span>{cursor ? '历史日志' : '最新日志 · 自动更新'}</span>
        <NativeButton
          disabled={!cursor}
          onClick={() => {
            setCursor(null);
            setHistory([]);
          }}
        >
          返回最新
        </NativeButton>
        <NativeButton
          disabled={!history.length}
          onClick={() => {
            setCursor(history.at(-1));
            setHistory((h) => h.slice(0, -1));
          }}
        >
          上一页
        </NativeButton>
        <NativeButton
          disabled={!next || loading}
          onClick={() => {
            setHistory((h) => [...h, cursor]);
            setCursor(next);
          }}
        >
          下一页
        </NativeButton>
      </div>
    </section>
  );
}
