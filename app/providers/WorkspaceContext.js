'use client';
import { createContext, useContext, useEffect, useRef, useState, useMemo, useCallback } from 'react';
import { updateMatches } from '../lib/data-updates.mjs';
import { beijingDay, watchBeijingDay } from '../lib/beijing-day.mjs';
import { clearSnapshots } from '../lib/response-snapshots.mjs';
import { recordLongTask, recordPerformance } from '../lib/ui-performance.mjs';
import AttentionNotice from '../components/overview/AttentionNotice';
import { fetchJson } from '../lib/fetch-json';
import { moduleNames } from '../components/shell/WorkspaceTools';
const Context = createContext(null);
const StatusContext = createContext(null);
export const useSyncStatus = () => useContext(StatusContext);
export function WorkspaceProvider({
  connected,
  children,
  detailedStatus = false,
  statusEnabled = true,
  attentionEnabled = true,
}) {
  useEffect(() => {
    try {
      const o = new PerformanceObserver((list) => list.getEntries().forEach((e) => recordLongTask(e.duration)));
      o.observe({ type: 'longtask', buffered: true });
      recordPerformance('页面启动', performance.now(), null);
      return () => o.disconnect();
    } catch {}
  }, []);
  const [businessDay, setBusinessDay] = useState(() => beijingDay());
  const automatic = useRef(true);
  useEffect(
    () =>
      watchBeijingDay((day) => {
        if (automatic.current) {
          clearSnapshots();
          setBusinessDay(day);
        }
      }),
    [],
  );
  const [auto, setAutoState] = useState(true),
    [autoReady, setAutoReady] = useState(false),
    [refreshVersion, setRefreshVersion] = useState(0);
  const pauseVersion = useRef(0),
    previousAuto = useRef(auto);
  if (previousAuto.current !== auto) {
    if (!auto) pauseVersion.current++;
    previousAuto.current = auto;
  }
  automatic.current = auto;
  const refreshMode = useRef('read');
  useEffect(() => {
    if (auto && autoReady) {
      setBusinessDay(beijingDay());
      window.dispatchEvent(new CustomEvent('database-updated', { detail: { reset: true } }));
    }
  }, [auto, autoReady]);
  const [theme, setThemeState] = useState('white');
  useEffect(() => {
    try {
      const saved = localStorage.getItem('pdd-manager:theme');
      const t = ['white', 'blue', 'dark'].includes(saved) ? saved : 'white';
      setThemeState(t);
      document.documentElement.dataset.theme = t;
      localStorage.setItem('pdd-manager:theme', t);
    } catch {}
  }, []);
  const setTheme = useCallback((t) => {
    setThemeState(t);
    document.documentElement.dataset.theme = t;
    try {
      localStorage.setItem('pdd-manager:theme', t);
    } catch {}
  }, []);
  const [live, setLive] = useState(false);
  useEffect(() => {
    if (!connected) return;
    const stream = new EventSource('/api/events');
    let last = null,
      timer,
      pending = null;
    const notify = (detail) => {
      if (document.hidden || !automatic.current) return;
      pending =
        pending?.reset || !detail?.changes
          ? { reset: true }
          : pending
            ? { ...detail, changes: [...pending.changes, ...detail.changes] }
            : detail;
      if (!timer)
        timer = setTimeout(() => {
          timer = null;
          const value = pending;
          pending = null;
          window.dispatchEvent(new CustomEvent('database-updated', { detail: value }));
        }, 350);
    };
    stream.addEventListener('database', (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.version !== last) {
          last = data.version;
          notify(data);
        }
        setLive(true);
      } catch {}
    });
    stream.onerror = () => setLive(false);
    const periodic = setInterval(() => {
      notify({ reset: true });
    }, 60000);
    const visible = () => notify({ reset: true });
    document.addEventListener('visibilitychange', visible);
    return () => {
      stream.close();
      clearInterval(periodic);
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', visible);
      setLive(false);
    };
  }, [connected]);
  const [status, setStatus] = useState(null),
    [statusError, setStatusError] = useState('');
  useEffect(() => {
    try {
      setAutoState(localStorage.getItem('pdd-manager:auto-refresh') !== 'false');
    } catch {}
    setAutoReady(true);
    const update = (e) => {
      if (e.key === 'pdd-manager:auto-refresh') setAutoState(e.newValue !== 'false');
    };
    window.addEventListener('storage', update);
    return () => window.removeEventListener('storage', update);
  }, []);
  const setAuto = useCallback((value) => {
    setAutoState(value);
    try {
      localStorage.setItem('pdd-manager:auto-refresh', String(value));
    } catch {}
  }, []);
  useEffect(() => {
    if (!connected || !statusEnabled) {
      setStatus(null);
      return;
    }
    let active = true,
      busy = false,
      last = 0,
      idle = false;
    const c = new AbortController();
    async function load() {
      if (busy || document.hidden || (idle && Date.now() - last < 30000)) return;
      busy = true;
      try {
        const d = await fetchJson(detailedStatus ? '/api/system/progress' : '/api/system/summary', {
          signal: c.signal,
        });
        last = Date.now();
        idle = !d.summary?.running && !d.summary?.queued;
        if (active) {
          setStatus((old) => (JSON.stringify(old) === JSON.stringify(d) ? old : d));
          setStatusError('');
        }
      } catch (e) {
        if (active && e.name !== 'AbortError') setStatusError('同步状态连接中断');
      } finally {
        busy = false;
      }
    }
    load();
    document.addEventListener('visibilitychange', load);
    const timer = setInterval(load, 5000);
    return () => {
      active = false;
      c.abort();
      clearInterval(timer);
      document.removeEventListener('visibilitychange', load);
    };
  }, [connected, detailedStatus, statusEnabled]);
  const refreshNow = useCallback((mode = 'read') => {
    refreshMode.current = mode;
    setRefreshVersion((v) => v + 1);
  }, []);
  const value = useMemo(
    () => ({
      businessDay,
      pauseVersion,
      live,
      theme,
      setTheme,
      auto,
      autoReady,
      setAuto,
      refreshVersion,
      refreshMode,
      refreshNow,
    }),
    [businessDay, live, theme, setTheme, auto, autoReady, setAuto, refreshVersion, refreshNow],
  );
  const progress = useMemo(() => ({ status, statusError }), [status, statusError]);
  return (
    <StatusContext.Provider value={progress}>
      <Context.Provider value={value}>
        <AttentionNotice connected={connected && attentionEnabled}>{children}</AttentionNotice>
      </Context.Provider>
    </StatusContext.Provider>
  );
}
export const useWorkspace = () => useContext(Context);
export function useDatabaseUpdates(callback, scope = {}) {
  const workspace = useWorkspace(),
    action = useRef(callback),
    filter = useRef(scope),
    enabled = useRef(workspace.auto);
  action.current = callback;
  filter.current = scope;
  enabled.current = workspace.auto;
  useEffect(() => {
    const changed = (e) => {
      if (enabled.current && !document.hidden && updateMatches(e.detail, filter.current)) action.current();
    };
    window.addEventListener('database-updated', changed);
    return () => window.removeEventListener('database-updated', changed);
  }, []);
}
export function useGlobalRefresh(callback) {
  const { refreshVersion, refreshMode } = useWorkspace();
  const last = useRef(refreshVersion),
    action = useRef(callback);
  action.current = callback;
  useEffect(() => {
    if (last.current !== refreshVersion) {
      last.current = refreshVersion;
      action.current(refreshMode.current);
    }
  }, [refreshVersion]);
}
export function GlobalSyncBar({ connected, view, onNavigate, platform = 'pdd' }) {
  const [submitted, setSubmitted] = useState([]);
  useEffect(() => {
    setSubmitted([]);
  }, [view]);
  useEffect(() => {
    const report = (e) => setSubmitted((old) => [...old.filter((r) => r.path !== e.detail.path), e.detail]);
    window.addEventListener('collection-submitted', report);
    return () => window.removeEventListener('collection-submitted', report);
  }, []);
  const { auto, autoReady, setAuto, refreshNow } = useWorkspace();
  const { status, statusError } = useSyncStatus();
  const s = status?.summary;
  const business = !['shops', 'browser_workspace', 'logs', 'sources', 'settings', 'machines', 'platform'].includes(
    view,
  );
  return (
    <div className="global-sync-bar">
      <div className="global-sync-info">
        <span
          className={`global-status-dot ${!connected || statusError || status?.collector_online === false ? 'warning' : s?.running ? 'working' : ''}`}
        />
        <strong>
          {!connected
            ? '服务未连接'
            : statusError
              ? statusError
              : !status
                ? '读取同步状态…'
                : status.collector_online === false
                  ? '采集服务离线 · 可查看已存数据'
                  : s.running
                    ? `${s.running} 项同步中`
                    : s.queued
                      ? `${s.queued} 项排队中`
                      : '同步队列空闲'}
        </strong>
        <button onClick={() => onNavigate('sources')}>{s?.errors ? `${s.errors} 项读取异常` : '同步中心'} ↗</button>
      </div>
      <div className="global-sync-actions">
        <button className="global-auto-label" disabled={!autoReady} onClick={() => setAuto(!auto)}>
          {auto ? '界面自动更新' : '界面已暂停'}
        </button>
        {business && (
          <button
            className="primary"
            disabled={!connected || !autoReady}
            onClick={() => refreshNow('read')}
            title="只读取数据库，不采集店铺后台"
          >
            刷新已存数据
          </button>
        )}
        {business && view !== 'fleet' && (
          <button
            disabled={!connected || !autoReady}
            onClick={() => {
              setSubmitted([]);
              refreshNow('collect');
            }}
            title="将此页筛选范围加入采集队列；首页包含经营、交易、推广和重点模块"
          >
            采集当前范围
          </button>
        )}
        {submitted.length > 0 && (
          <small role="status" className="command-result">
            {submitted
              .map((r) => (moduleNames[r.path.split('/').at(-1)] || '重点事项') + (r.ok ? '已提交' : '提交失败'))
              .join(' · ')}
            （当前筛选范围）
          </small>
        )}
      </div>
    </div>
  );
}
