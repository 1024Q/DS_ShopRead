'use client';
import { NativeButton, TextInput, Choice } from './components/shared/AdminControls';
import dynamic from 'next/dynamic';
import ShopMachineGroups from './components/shops/ShopMachineGroups';
import DialogBehavior from './components/shared/DialogBehavior';
import { moduleNames } from './components/shell/WorkspaceTools';
import AdminDialog from './components/shared/AdminDialog';
import HomeTheme from './providers/HomeTheme';
import { AdminWorkspaceContext } from './providers/AdminWorkspace';
import MultiSelect from './components/shared/MultiSelect';
import usePersistentSelection from './hooks/usePersistentSelection';
import { inScope, selectedValues } from './lib/filter-scope.mjs';
import { throttleUpdates } from './lib/data-updates.mjs';
import { PlatformFilterContext } from './components/shared/PlatformFilter';
import { shopLabel } from './lib/shop-identity.mjs';
import { sortShops, shopSortOptions } from './lib/shop-sort.mjs';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { fetchJson, shopStateLabels } from './lib/fetch-json';
const BrowserWorkspace = dynamic(() => import('./components/shops/BrowserWorkspace'));
const ReportExports = dynamic(() => import('./components/reconciliation/ReportExports'));
const CostTables = dynamic(() => import('./components/reconciliation/CostTables'));
const ReportView = dynamic(() => import('./components/reconciliation/ReportView'));
const BusinessPanel = dynamic(() => import('./components/business/BusinessPanel'), {
  loading: () => (
    <p role="status" className="module-loading">
      正在加载模块…
    </p>
  ),
});
import { setSnapshotScope, clearSnapshots } from './lib/response-snapshots.mjs';
const PromotionsPanel = dynamic(() => import('./components/business/PromotionsPanel'), {
  loading: () => (
    <p role="status" className="module-loading">
      正在加载模块…
    </p>
  ),
});
import { PendingBusiness } from './components/business/BusinessScope';
import { hasBusinessAdapter, systemViews, exclusivePlatforms, canReadKind } from './lib/platforms.mjs';
import Navigation from './components/shell/Navigation';
import LanguageSwitcher from './i18n/LanguageSwitcher';
import permissions from '../lib/ui-permissions.cjs';
import SessionMenu from './components/auth/SessionMenu';
import { setClientAccess } from './lib/client-access.mjs';
const AccountManager = dynamic(() => import('./components/auth/AccountManager'));
const FleetDashboard = dynamic(() => import('./components/collection/FleetDashboard'), {
  loading: () => (
    <p role="status" className="module-loading">
      正在加载模块…
    </p>
  ),
});
const LogPanel = dynamic(() => import('./components/collection/LogPanel'), {
  loading: () => (
    <p role="status" className="module-loading">
      正在加载模块…
    </p>
  ),
});
const TodoPanel = dynamic(() => import('./components/overview/TodoPanel'), {
  loading: () => (
    <p role="status" className="module-loading">
      正在加载模块…
    </p>
  ),
});
const ProductOverview = dynamic(() => import('./components/business/ProductOverview'), {
  loading: () => (
    <p role="status" className="module-loading">
      正在加载模块…
    </p>
  ),
});
const ShopImport = dynamic(() => import('./components/shops/ShopImport'), {
  loading: () => (
    <p role="status" className="module-loading">
      正在加载模块…
    </p>
  ),
});
const OperatorManager = dynamic(() => import('./components/shops/OperatorManager'), {
  loading: () => (
    <p role="status" className="module-loading">
      正在加载模块…
    </p>
  ),
});
import ShopCard from './components/shops/ShopCard';
import ShopNodeSelect from './components/shops/ShopNodeSelect';
import MoveShopNode from './components/shops/MoveShopNode';
import { useShopHealth } from './components/shops/ShopHealth';
const SyncCenter = dynamic(() => import('./components/collection/SyncCenter'), {
  loading: () => (
    <p role="status" className="module-loading">
      正在加载模块…
    </p>
  ),
});
import { platforms } from './lib/platforms.mjs';
const SystemSettings = dynamic(() => import('./components/settings/SystemSettings'));
import RemoteMachines, { MachinePicker } from './components/settings/RemoteMachines';
const Viewer = dynamic(() => import('./components/shell/RemoteViewer'), {
  loading: () => (
    <p role="status" className="module-loading">
      正在加载模块…
    </p>
  ),
});
import { ShopOperatorsProvider } from './providers/ShopOperators';
import { WorkspaceProvider, GlobalSyncBar } from './providers/WorkspaceContext';

async function api(path, body, method = body === undefined ? 'GET' : 'POST') {
  if (method !== 'GET') clearSnapshots();
  return fetchJson('/api/' + path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}
// Single source for views restorable from localStorage / URL.
const ProfitCenter = dynamic(() => import('./components/reconciliation/ProfitCenter'));
const TaxCenter = dynamic(() => import('./components/reconciliation/TaxCenter'));
const PromotionLedger = dynamic(() => import('./components/reconciliation/PromotionLedger'));
const knownViews = new Set([
  'accounts',
  'profit_tax',
  'profit',
  'promotion_ledger',
  'cost_tables',
  'report_view',
  'report_exports',
  'overview',
  ...Object.keys(moduleNames),
  'todos',
  'shops',
  'browser_workspace',
  'logs',
  'sources',
  'settings',
  'machines',
  'fleet',
]);
const windowActions = [
  ['show', '显示所有窗口'],
  ['hide', '隐藏所有窗口'],
  ['tile', '平铺所有窗口'],
];
function WindowMenu({ disabled, pending, onAction }) {
  const menu = useRef(null),
    [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const close = (e) => {
      if (e.type === 'keydown') {
        if (e.key === 'Escape') {
          menu.current.open = false;
          menu.current.querySelector('summary').focus();
        }
      } else if (!menu.current?.contains(e.target)) menu.current.open = false;
    };
    document.addEventListener('pointerdown', close);
    document.addEventListener('keydown', close);
    return () => {
      document.removeEventListener('pointerdown', close);
      document.removeEventListener('keydown', close);
    };
  }, [open]);
  return (
    <details
      ref={menu}
      className="shop-card-menu shop-window-menu"
      style={{ marginLeft: 0 }}
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary aria-label="窗口操作">
        {pending ? '窗口处理中…' : '窗口'} <span aria-hidden="true">⌄</span>
      </summary>
      <div className="shop-card-menu-panel">
        <strong>窗口</strong>
        <p>作用于当前工作机器上所有已启动的店铺浏览器，不受平台筛选影响。</p>
        <div className="shop-card-menu-actions">
          {windowActions.map(([action, label]) => (
            <NativeButton
              key={action}
              disabled={disabled}
              onClick={() => {
                menu.current.open = false;
                onAction(action);
              }}
            >
              {pending === action ? '处理中…' : label}
            </NativeButton>
          ))}
        </div>
      </div>
    </details>
  );
}

export default function Page() {
  const [user, setUser] = useState(null);
  const [businessPlatform, setBusinessPlatform, platformReady] = usePersistentSelection('business-platform');
  const navigationUrl = useRef('');
  const [view, setView] = useState('overview'),
    [viewReady, setViewReady] = useState(false),
    [initialFilters, setInitialFilters] = useState(null);
  const effectivePlatform = exclusivePlatforms[view] || businessPlatform;
  useEffect(() => {
    try {
      const saved = localStorage.getItem('pdd-manager:view');
      if (knownViews.has(saved)) setView(saved);
    } catch {}
    try {
      const target = JSON.parse(sessionStorage.getItem('commerce:navigate') || 'null');
      if (target && ['overview', 'todos'].includes(target.view)) {
        setView(target.view);
        setInitialFilters(target.filters);
        setBusinessPlatform(target.platform || '');
        sessionStorage.removeItem('commerce:navigate');
      }
    } catch {}
    setViewReady(true);
  }, []);
  useEffect(() => {
    if (viewReady)
      try {
        localStorage.setItem('pdd-manager:view', view);
      } catch {}
  }, [view, viewReady]);
  const navigate = (kind, filters = null) => {
    if (!window.dispatchEvent(new Event('workspace-before-navigate', { cancelable: true }))) return;
    const url = new URL(location.href);
    url.searchParams.set('view', kind);
    if (filters) url.searchParams.set('filters', JSON.stringify(filters));
    else url.searchParams.delete('filters');
    history.pushState(null, '', url);
    navigationUrl.current = url.href;
    setInitialFilters(filters);
    if (filters?.shop_id) {
      setBusinessPlatform(
        [...new Set(shops.filter((s) => inScope(filters.shop_id, s.id)).map((s) => s.platform || 'pdd'))]
          .sort()
          .join(','),
      );
    } else if (filters?.platform) setBusinessPlatform(filters.platform);
    else if (filters) setBusinessPlatform('');
    setView(kind);
    window.scrollTo(0, 0);
  };
  useEffect(() => {
    const restore = (event) => {
      if (event && !window.dispatchEvent(new Event('workspace-before-navigate', { cancelable: true }))) {
        history.pushState(null, '', navigationUrl.current);
        return;
      }
      navigationUrl.current = location.href;
      const q = new URLSearchParams(location.search),
        v = q.get('view');
      if (!v || !knownViews.has(v)) return;
      setView(v);
      try {
        const f = JSON.parse(q.get('filters') || 'null');
        setInitialFilters(f);
        if (f?.platform !== undefined) setBusinessPlatform(f.platform);
        else if (f?.shop_id) setBusinessPlatform('');
      } catch {}
      window.scrollTo(0, 0);
    };
    restore();
    window.addEventListener('popstate', restore);
    return () => window.removeEventListener('popstate', restore);
  }, []);
  useEffect(() => {
    const update = () => {
      navigationUrl.current = location.href;
    };
    window.addEventListener('workspace-query-location', update);
    return () => window.removeEventListener('workspace-query-location', update);
  }, []);
  const openSource = async (id, kind) => {
    const shop = shops.find((s) => s.id === id);
    if (shop && (await operate(shop, kind))) setViewer(id);
  };
  const [shops, setShops] = useState([]),
    [browsers, setBrowsers] = useState([]);
  const [connected, setConnected] = useState(false),
    [connectionInfo, setConnectionInfo] = useState(null);
  const shopHealth = useShopHealth(connected && view === 'shops');
  const [message, setMessage] = useState(''),
    [busy, setBusy] = useState({});
  const [operatorManager, setOperatorManager] = useState(false),
    [operatorNames, setOperatorNames] = useState([]);
  const [operator, setOperator] = useState(''),
    [platform, setPlatform] = usePersistentSelection('shop-management-platform'),
    [machineVersion, setMachineVersion] = useState(0);
  const [shopSearch, setShopSearch] = useState(''),
    [shopState, setShopState] = useState(''),
    [shopLayout, setShopLayout] = useState('cards'),
    [shopSort, setShopSort] = useState('attention'),
    [batchProgress, setBatchProgress] = useState(null);
  useEffect(() => {
    try {
      const saved = localStorage.getItem('commerce:shop-sort');
      if (shopSortOptions.some(([id]) => id === saved)) setShopSort(saved);
    } catch {}
  }, []);
  const changeShopSort = (value) => {
    setShopSort(value);
    try {
      localStorage.setItem('commerce:shop-sort', value);
    } catch {}
  };
  const visibleShops = useMemo(
    () =>
      sortShops(
        shops.filter(
          (s) =>
            inScope(platform, s.platform) &&
            (!operator ||
              (operator === '__none__'
                ? !(s.operators || [s.operator].filter(Boolean)).length
                : (s.operators || [s.operator]).includes(operator))) &&
            (!shopState || s.state === shopState) &&
            (!shopSearch || `${shopLabel(s)} ${s.username || ''}`.toLowerCase().includes(shopSearch.toLowerCase())),
        ),
        shopSort,
      ),
    [shops, platform, operator, shopState, shopSearch, shopSort],
  );
  const healthMap = useMemo(() => new Map((shopHealth.data?.rows || []).map((r) => [r.shop_id, r])), [shopHealth.data]);
  const operatorOptions = useMemo(
    () => [...new Set(shops.flatMap((s) => s.operators || [s.operator].filter(Boolean)))].sort(),
    [shops],
  );
  const clearShopFilters = () => {
    setShopSearch('');
    setShopState('');
    setOperator('');
    setPlatform('');
  };
  useEffect(() => {
    if (!batchProgress || batchProgress.done < batchProgress.total) return;
    const timer = setTimeout(() => setBatchProgress(null), 5000);
    return () => clearTimeout(timer);
  }, [batchProgress]);
  async function startSelected() {
    const pending = visibleShops.filter((s) => s.adapter_available !== false && s.state === 'stopped');
    if (!pending.length) return;
    let next = 0,
      done = 0,
      failed = 0;
    setBatchProgress({ done, total: pending.length, failed });
    async function worker() {
      while (next < pending.length) {
        const shop = pending[next++];
        if (!(await operate(shop, 'start', true))) failed++;
        done++;
        setBatchProgress({ done, total: pending.length, failed });
      }
    }
    await Promise.all([worker(), worker()]);
    await refresh();
  }
  const [windowAction, setWindowAction] = useState('');
  const [importing, setImporting] = useState(false);
  const [shopNode, setShopNode] = useState(''),
    [centerMode, setCenterMode] = useState(false),
    [movingShop, setMovingShop] = useState(null),
    [movingTarget, setMovingTarget] = useState('');
  const [adding, setAdding] = useState(false),
    [viewer, setViewer] = useState(null);
  const [editing, setEditing] = useState(null),
    [deleting, setDeleting] = useState(null);
  const [saving, setSaving] = useState(false),
    [formError, setFormError] = useState('');
  const [form, setForm] = useState({
    username: '',
    password: '',
    name: '',
    browser: 'system',
    platform: 'pdd',
    operator: '',
    login_method: 'password',
    login_url: '',
  });
  useEffect(() => {
    const report = (e) => {
      const message =
        e.type === 'unhandledrejection' ? '前端未处理的异步错误' : `前端脚本错误：${e.message || '未知错误'}`;
      fetch('/api/logs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: message.slice(0, 500) }),
      }).catch(() => {});
    };
    window.addEventListener('error', report);
    window.addEventListener('unhandledrejection', report);
    return () => {
      window.removeEventListener('error', report);
      window.removeEventListener('unhandledrejection', report);
    };
  }, []);
  const seenVerification = useRef(new Set());
  useEffect(() => {
    const open = (e) => navigate(e.detail.view);
    window.addEventListener('workspace-navigate', open);
    return () => window.removeEventListener('workspace-navigate', open);
  }, []);
  const refresh = useCallback(async () => {
    try {
      const records = await api('shops');
      setShops((old) => (JSON.stringify(old) === JSON.stringify(records) ? old : records));
    } catch (error) {
      setMessage(error.message);
    }
  }, []);
  useEffect(() => {
    api('connection')
      .then((config) => {
        if (config.center && !config.connected) {
          location.replace('/center');
          return;
        }
        setConnectionInfo(config);
        setUser(config.user);
        setClientAccess(config.user);
        setCenterMode(!!config.center);
        setSnapshotScope(config.cacheScope);
        setConnected(config.connected);
      })
      .catch((error) => setMessage(error.message));
  }, []);
  useEffect(() => {
    if (!connected) return;
    // Database-updated events (SSE via WorkspaceContext) trigger immediate refreshes; the interval is only a fallback.
    const visible = () => {
      if (!document.hidden) refresh();
    };
    // Merge bursts (≤ 1 refresh / 2 s) but keep a trailing refresh so the
    // last change of a burst is never dropped.
    const changed = throttleUpdates(visible, 2000);
    visible();
    document.addEventListener('visibilitychange', visible);
    window.addEventListener('database-updated', changed);
    const timer = setInterval(visible, ['shops', 'browser_workspace'].includes(view) ? 20000 : 60000);
    return () => {
      clearInterval(timer);
      changed.cancel();
      document.removeEventListener('visibilitychange', visible);
      window.removeEventListener('database-updated', changed);
    };
  }, [connected, refresh, view]);
  useEffect(() => {
    if (!connected || view !== 'shops') return;
    let active = true;
    const load = () =>
      document.hidden
        ? Promise.resolve()
        : Promise.all([api('browsers'), api('operators')])
            .then(([available, people]) => {
              if (active) {
                setBrowsers(available);
                setOperatorNames(people);
              }
            })
            .catch(() => {});
    load();
    const timer = setInterval(load, 30000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [connected, view, machineVersion]);
  async function operate(shop, action, batch = false) {
    setBusy((previous) => ({ ...previous, [shop.id]: true }));
    setMessage('');
    try {
      await api(`shops/${shop.id}/${action}`, {});
      if (action === 'show' || action === 'qr-login') setViewer(shop.id);
      if (!batch) await refresh();
      return true;
    } catch (error) {
      setMessage(error.message);
      return false;
    } finally {
      setBusy((previous) => ({ ...previous, [shop.id]: false }));
    }
  }
  async function assignOperator(ids, names, mode = 'replace') {
    setBusy((old) => ({ ...old, ...Object.fromEntries(ids.map((id) => [id, true])) }));
    try {
      await api('shops/operators', {
        shop_ids: ids,
        operators: Array.isArray(names) ? names : [names].filter(Boolean),
        mode,
      });
      await refresh();
    } finally {
      setBusy((old) => ({ ...old, ...Object.fromEntries(ids.map((id) => [id, false])) }));
    }
  }
  async function allWindows(action) {
    if (windowAction) return;
    setWindowAction(action);
    setMessage('');
    try {
      const result = await api('shops/windows', { action });
      const title = { show: '显示', hide: '隐藏', tile: '平铺' }[action];
      const failed = result.results.filter((r) => !r.ok);
      setMessage(
        `已${title} ${result.count} 家店铺的窗口${action === 'tile' ? '，按主屏幕可用区域排列' : ''}。` +
          (failed.length
            ? ` ${failed.length} 家未完成：` +
              failed
                .map((r) => `${shopLabel(shops.find((s) => s.id === r.shop_id) || { id: r.shop_id })}：${r.error}`)
                .join('；')
            : ''),
      );
      await refresh();
    } catch (error) {
      setMessage(error.message);
    } finally {
      setWindowAction('');
    }
  }
  function openEditor(shop = null) {
    setShopNode('');
    setEditing(shop);
    setFormError('');
    setForm({
      username: shop?.username || '',
      password: '',
      name: shop?.alias || '',
      browser: shop?.browser || 'system',
      platform: shop?.platform || selectedValues(platform)[0] || 'pdd',
      operator: shop?.operator || '',
      login_method: (shop?.platform || platform) === 'wechat' ? 'qr' : shop?.login_method || 'password',
      login_url: shop?.login_url || '',
    });
    setAdding(true);
  }
  function closeEditor() {
    if (saving) return;
    setAdding(false);
    setEditing(null);
    setForm({
      username: '',
      password: '',
      name: '',
      browser: 'system',
      platform: 'pdd',
      operator: '',
      login_method: 'password',
      login_url: '',
    });
    setFormError('');
  }
  async function saveShop(event) {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setFormError('');
    try {
      if (!editing && centerMode && !shopNode) throw Error('请选择所属采集节点');
      const saved = await api(
        editing ? `shops/${editing.id}` : shopNode ? `nodes/${shopNode}/proxy/shops` : 'shops',
        form,
        editing ? 'PUT' : 'POST',
      );
      if (!editing && shopNode) {
        await api('active-node', { id: 0 });
        setMachineVersion((v) => v + 1);
      }
      setShops((previous) =>
        editing ? previous.map((shop) => (shop.id === saved.id ? saved : shop)) : [...previous, saved],
      );
      setAdding(false);
      setEditing(null);
      setForm({
        username: '',
        password: '',
        name: '',
        browser: 'system',
        platform: 'pdd',
        operator: '',
        login_method: 'password',
        login_url: '',
      });
      await refresh();
      if (!editing && saved.login_method === 'qr') {
        await operate(saved, 'qr-login');
        setViewer(saved.id);
      }
    } catch (error) {
      setFormError(error.message);
    } finally {
      setSaving(false);
    }
  }
  async function deleteShop() {
    if (!deleting || saving) return;
    setSaving(true);
    setFormError('');
    try {
      await api(`shops/${deleting.id}`, undefined, 'DELETE');
      setShops((previous) => previous.filter((shop) => shop.id !== deleting.id));
      setViewer((current) => (current === deleting.id ? null : current));
      seenVerification.current.delete(deleting.id);
      setDeleting(null);
      await refresh();
    } catch (error) {
      setFormError(error.message);
    } finally {
      setSaving(false);
    }
  }
  const selected = shops.find((shop) => shop.id === viewer);
  return (
    <WorkspaceProvider
      connected={connected}
      detailedStatus={view === 'sources'}
      statusEnabled={['sources', 'fleet', 'settings'].some((p) => permissions.can(user, p))}
      attentionEnabled={permissions.can(user, 'todos')}
    >
      <ShopOperatorsProvider shops={shops}>
        <AdminWorkspaceContext.Provider value={true}>
          <HomeTheme active={view === 'overview'}>
            <main
              data-admin={view !== 'overview'}
              className={`app-shell ${['settings', 'sources', 'shops', 'browser_workspace', 'logs', 'machines', 'fleet'].includes(view) ? 'system-workspace' : ''}`}
            >
              <Navigation
                view={view}
                user={user}
                onChange={(target) => {
                  if (permissions.can(user, target)) navigate(target);
                }}
              />
              <>
                {operatorManager && (
                  <OperatorManager
                    initialSelected={Array.isArray(operatorManager) ? operatorManager : []}
                    onBatchSave={async (names) => {
                      setOperatorNames(await api('operators', { names }));
                      await refresh();
                    }}
                    shops={shops}
                    names={operatorNames}
                    onSave={async (name, previous) => {
                      setOperatorNames(await api('operators', { name, previous }));
                      await refresh();
                    }}
                    onAssign={assignOperator}
                    onClose={() => setOperatorManager(false)}
                  />
                )}
              </>
              <div className="app-main">
                <header className="workspace-header unified-header" aria-label="工作台顶部工具栏">
                  <div className="header-actions">
                    <span className="header-shop-count">
                      {shops.filter((s) => s.state === 'logged_in').length} / {shops.length} 家已登录
                    </span>
                    {connected && <MachinePicker key={machineVersion} />}
                    <LanguageSwitcher />
                    <SessionMenu user={user} />
                    {user && !permissions.can(user, view, true) && <span>只读权限</span>}
                  </div>
                  <DialogBehavior />
                  <GlobalSyncBar connected={connected} view={view} onNavigate={navigate} />
                </header>
                <PlatformFilterContext.Provider
                  value={{ view, platform: effectivePlatform, onChange: setBusinessPlatform }}
                >
                  <div className="page-content">
                    {!platformReady || !user ? null : !permissions.can(user, view) ? (
                      <p role="alert">没有此页面权限，请在左侧选择已授权页面。</p>
                    ) : view === 'accounts' ? (
                      <AccountManager />
                    ) : view === 'browser_workspace' ? (
                      <BrowserWorkspace
                        shops={shops}
                        connected={connected}
                        busy={busy}
                        onOperate={operate}
                        onView={(shop) => setViewer(shop.id)}
                        onRefresh={refresh}
                        onAll={async () => {
                          if (!window.dispatchEvent(new Event('workspace-before-navigate', { cancelable: true })))
                            return;
                          try {
                            await api('active-node', { id: 0 });
                            window.location.reload();
                          } catch (e) {
                            setMessage(e.message);
                          }
                        }}
                      />
                    ) : view === 'shops' ? (
                      <>
                        <section className="summary shop-summary">
                          <div>
                            <span className="eyebrow">当前工作机器</span>
                            <strong>店铺管理</strong>
                          </div>
                          <div>
                            <strong>{shops.length}</strong>
                            <span>家店铺</span>
                          </div>
                          <div>
                            <strong>{shops.filter((shop) => shop.state === 'logged_in').length}</strong>
                            <span>家已登录</span>
                          </div>
                          <NativeButton className="primary" onClick={() => openEditor()} disabled={!connected}>
                            ＋ 添加店铺
                          </NativeButton>
                          <NativeButton disabled={!connected} onClick={() => setImporting(true)}>
                            批量导入店铺
                          </NativeButton>
                        </section>
                        {message && (
                          <div className="notice" role="alert">
                            {message}
                            <NativeButton aria-label="关闭提示" onClick={() => setMessage('')}>
                              ×
                            </NativeButton>
                          </div>
                        )}
                        <div className="section-head shop-section-head">
                          <h2>我的店铺</h2>
                          <div className="actions shop-toolbar">
                            <div className="shop-filter-group" role="group" aria-label="店铺筛选">
                              <TextInput
                                className="shop-search"
                                aria-label="搜索店铺"
                                placeholder="店铺名称 / 账号"
                                value={shopSearch}
                                onChange={(e) => setShopSearch(e.target.value)}
                              />
                              <label>
                                运营负责人
                                <Choice
                                  aria-label="运营负责人筛选"
                                  value={operator}
                                  onChange={(e) => setOperator(e.target.value)}
                                >
                                  <option value="">全部人员</option>
                                  <option value="__none__">未分配</option>
                                  {operatorOptions.map((name) => (
                                    <option key={name}>{name}</option>
                                  ))}
                                </Choice>
                              </label>
                              <label>
                                平台分类
                                <MultiSelect
                                  label="平台分类"
                                  value={platform}
                                  onChange={(value) => setPlatform(value)}
                                  options={Object.entries(platforms).map(([value, label]) => ({ value, label }))}
                                  placeholder="全部平台"
                                />
                              </label>
                              <Choice
                                aria-label="店铺登录状态"
                                value={shopState}
                                onChange={(e) => setShopState(e.target.value)}
                              >
                                <option value="">全部登录状态</option>
                                {Object.entries(shopStateLabels).map(([id, label]) => (
                                  <option key={id} value={id}>
                                    {label}
                                  </option>
                                ))}
                              </Choice>
                              <label>
                                排序
                                <Choice
                                  aria-label="店铺排序"
                                  value={shopSort}
                                  onChange={(e) => changeShopSort(e.target.value)}
                                >
                                  {shopSortOptions.map(([id, label]) => (
                                    <option key={id} value={id}>
                                      {label}
                                    </option>
                                  ))}
                                </Choice>
                              </label>
                            </div>
                            <div className="shop-action-group" role="group" aria-label="店铺操作">
                              <NativeButton
                                disabled={
                                  !connected ||
                                  !!windowAction ||
                                  Object.values(busy).some(Boolean) ||
                                  !visibleShops.some((s) => s.state === 'stopped' && s.adapter_available !== false)
                                }
                                onClick={startSelected}
                              >
                                启动筛选中未启动的店铺
                              </NativeButton>
                              {batchProgress && (
                                <span role="status">
                                  {batchProgress.done < batchProgress.total ? '启动中' : '启动完成'}{' '}
                                  {batchProgress.done}/{batchProgress.total} · 失败 {batchProgress.failed}
                                </span>
                              )}
                              <WindowMenu
                                pending={windowAction}
                                disabled={
                                  !connected ||
                                  !!windowAction ||
                                  Object.values(busy).some(Boolean) ||
                                  !shops.some(
                                    (s) => s.state !== 'stopped' && !s.headless && s.adapter_available !== false,
                                  )
                                }
                                onAction={allWindows}
                              />
                              <NativeButton
                                disabled={!connected || !visibleShops.length}
                                onClick={() => {
                                  setMovingTarget('');
                                  setMovingShop(visibleShops);
                                }}
                              >
                                批量切换节点
                              </NativeButton>
                              <NativeButton onClick={() => setOperatorManager(true)}>
                                管理运营人员 / 批量分配
                              </NativeButton>
                              <NativeButton onClick={() => setShopLayout((v) => (v === 'cards' ? 'list' : 'cards'))}>
                                {shopLayout === 'cards' ? '紧凑列表' : '卡片视图'}
                              </NativeButton>
                            </div>
                          </div>
                        </div>
                        <ShopMachineGroups
                          shops={visibleShops}
                          layout={shopLayout}
                          onMove={(shop, target) => {
                            setMovingTarget(String(target));
                            setMovingShop(shop);
                          }}
                        >
                          {(shop) => (
                            <ShopCard
                              health={healthMap.get(shop.id)}
                              healthError={shopHealth.error}
                              operatorNames={operatorNames}
                              onAddOperator={() => setOperatorManager([shop.id])}
                              onOperator={(name) => assignOperator([shop.id], name).catch((e) => setMessage(e.message))}
                              key={shop.id}
                              shop={shop}
                              busy={busy[shop.id] || !!windowAction}
                              saving={saving}
                              browsers={browsers}
                              onOperate={(action) => operate(shop, action)}
                              onView={() => setViewer(shop.id)}
                              onMove={() => {
                                setMovingTarget('');
                                setMovingShop(shop);
                              }}
                              onEdit={() => openEditor(shop)}
                              onDelete={() => {
                                setDeleting(shop);
                                setFormError('');
                              }}
                              onBrowser={async (browser) => {
                                try {
                                  await api(`shops/${shop.id}/browser`, { browser });
                                  await refresh();
                                } catch (error) {
                                  setMessage(error.message);
                                }
                              }}
                            />
                          )}
                        </ShopMachineGroups>
                        {!!shops.length && !visibleShops.length && (
                          <div className="empty">
                            <h3>没有匹配的店铺</h3>
                            <p>当前筛选条件下没有店铺。</p>
                            <NativeButton onClick={clearShopFilters}>清除筛选</NativeButton>
                          </div>
                        )}

                        {!shops.length && (
                          <div className="empty">
                            <h3>添加第一家店铺</h3>
                            <p>账号和密码保存在当前 Python 服务端的数据库中。</p>
                          </div>
                        )}
                        <footer>登录成功后自动识别店铺名称并隐藏窗口；遇到验证码时自动打开登录画面。</footer>
                      </>
                    ) : view !== 'overview' &&
                      view !== 'profit_tax' &&
                      view !== 'profit' &&
                      view !== 'promotion_ledger' &&
                      !systemViews.has(view) &&
                      effectivePlatform &&
                      !exclusivePlatforms[view] &&
                      !selectedValues(effectivePlatform).some((p) => hasBusinessAdapter(p, view)) ? (
                      <PendingBusiness
                        view={view}
                        platform={effectivePlatform}
                        onManage={() => {
                          setPlatform(effectivePlatform);
                          navigate('shops');
                        }}
                      />
                    ) : view === 'profit_tax' ? (
                      <TaxCenter />
                    ) : view === 'profit' ? (
                      <ProfitCenter />
                    ) : view === 'promotion_ledger' ? (
                      <PromotionLedger />
                    ) : view === 'cost_tables' ? (
                      <CostTables />
                    ) : view === 'report_view' ? (
                      <ReportView />
                    ) : view === 'report_exports' ? (
                      <ReportExports />
                    ) : view === 'fleet' ? (
                      <FleetDashboard />
                    ) : view === 'settings' ? (
                      <SystemSettings connected={connected} connection={connectionInfo} />
                    ) : connected && view === 'machines' ? (
                      <RemoteMachines
                        user={user}
                        connection={connectionInfo}
                        Viewer={Viewer}
                        onChanged={() => setMachineVersion((v) => v + 1)}
                      />
                    ) : connected && view === 'sources' ? (
                      <SyncCenter shops={shops} onOpen={openSource} onNavigate={navigate} />
                    ) : connected && view === 'logs' ? (
                      <LogPanel shops={shops} />
                    ) : connected && view === 'todos' ? (
                      <TodoPanel
                        initialFilters={initialFilters}
                        platform={effectivePlatform}
                        key={JSON.stringify(initialFilters)}
                        shops={shops.filter(
                          (s) => canReadKind(s, 'todos') && inScope(effectivePlatform, s.platform || 'pdd'),
                        )}
                        onNavigate={navigate}
                        onOpen={openSource}
                      />
                    ) : connected && ['ad_products', 'ad_reports'].includes(view) ? (
                      <PromotionsPanel
                        key={view + JSON.stringify(initialFilters)}
                        kind={view}
                        platform={effectivePlatform}
                        shops={shops.filter(
                          (s) => canReadKind(s, view) && inScope(effectivePlatform, s.platform || 'pdd'),
                        )}
                        initialFilters={initialFilters}
                        onOpen={openSource}
                      />
                    ) : connected && viewReady ? (
                      <BusinessPanel
                        key={view + JSON.stringify(initialFilters)}
                        platform={effectivePlatform}
                        initialFilters={initialFilters}
                        kind={view}
                        onChangeKind={navigate}
                        shops={
                          view === 'overview'
                            ? shops.filter((s) => inScope(effectivePlatform, s.platform || 'pdd'))
                            : shops.filter(
                                (s) => canReadKind(s, view) && inScope(effectivePlatform, s.platform || 'pdd'),
                              )
                        }
                        onOpen={openSource}
                      />
                    ) : (
                      <div className="empty">请先连接 Python API 服务。</div>
                    )}
                    {view !== 'shops' && message && (
                      <div className="notice" role="alert">
                        {message}
                        <NativeButton aria-label="关闭提示" onClick={() => setMessage('')}>
                          ×
                        </NativeButton>
                      </div>
                    )}
                  </div>
                </PlatformFilterContext.Provider>
              </div>
              {movingShop && (
                <MoveShopNode
                  onOpenLogin={(id) => {
                    setMovingShop(null);
                    setViewer(id);
                  }}
                  shop={movingShop}
                  initialNode={movingTarget}
                  onClose={() => {
                    setMovingShop(null);
                    setMovingTarget('');
                  }}
                  onMoved={async (node) => {
                    await api('active-node', { id: 0 });
                    setMachineVersion((v) => v + 1);
                    setMessage('节点切换已处理，登录成功后自动恢复采集；需要验证的店铺请完成验证');
                    await refresh();
                  }}
                />
              )}
              {importing && (
                <ShopImport
                  defaultPlatform={selectedValues(platform)[0] || 'pdd'}
                  onClose={() => setImporting(false)}
                  onImported={async (result, node) => {
                    if (node) {
                      await api('active-node', { id: 0 });
                      setMachineVersion((v) => v + 1);
                    }
                    await refresh();
                  }}
                />
              )}
              {adding && (
                <AdminDialog>
                  <form
                    className="admin-dialog-content shop-editor"
                    aria-label={editing ? '编辑店铺' : '添加店铺'}
                    onSubmit={saveShop}
                  >
                    <div className="dialog-head">
                      <h2>{editing ? '编辑店铺' : '添加店铺'}</h2>
                      <NativeButton
                        type="button"
                        disabled={saving}
                        aria-label="关闭"
                        data-dialog-close
                        onClick={closeEditor}
                      >
                        ×
                      </NativeButton>
                    </div>
                    {formError && (
                      <p className="error" role="alert">
                        {formError}
                      </p>
                    )}
                    {editing && (
                      <p className="form-hint">
                        {shopLabel(editing)}
                        {editing.machine_name ? ` · 所属节点：${editing.machine_name}` : ''}
                      </p>
                    )}
                    {!editing && (
                      <ShopNodeSelect
                        value={shopNode}
                        onChange={setShopNode}
                        disabled={saving}
                        onMode={setCenterMode}
                      />
                    )}
                    <label>
                      所属平台
                      <Choice
                        value={form.platform}
                        onChange={(e) =>
                          setForm({
                            ...form,
                            platform: e.target.value,
                            login_method: e.target.value === 'wechat' ? 'qr' : form.login_method,
                          })
                        }
                      >
                        {Object.entries(platforms).map(([k, v]) => (
                          <option key={k} value={k}>
                            {v}
                          </option>
                        ))}
                      </Choice>
                    </label>
                    {form.platform === 'other' && (
                      <label>
                        官方登录地址
                        <TextInput
                          type="url"
                          required
                          value={form.login_url || ''}
                          placeholder="https://官方平台登录地址"
                          onChange={(e) => setForm({ ...form, login_url: e.target.value })}
                        />
                      </label>
                    )}
                    <label>
                      店铺显示名称
                      <TextInput
                        value={form.name}
                        placeholder={editing?.detected_name || '留空则使用登录后识别的店铺名称'}
                        maxLength={100}
                        onChange={(e) => setForm({ ...form, name: e.target.value })}
                      />
                    </label>
                    <datalist id="operator-names">
                      {operatorNames.map((n) => (
                        <option key={n} value={n} />
                      ))}
                    </datalist>
                    <label>
                      运营负责人
                      <TextInput
                        list="operator-names"
                        aria-label="店铺运营负责人"
                        value={form.operator}
                        maxLength={10000}
                        placeholder="多位运营用逗号分隔"
                        onChange={(e) => setForm({ ...form, operator: e.target.value })}
                      />
                    </label>
                    <label>
                      登录方式
                      <Choice
                        aria-label="店铺登录方式"
                        value={form.login_method}
                        onChange={(e) => setForm({ ...form, login_method: e.target.value })}
                      >
                        <option value="password" disabled={form.platform === 'wechat'}>
                          账号密码登录{form.platform === 'wechat' ? '（官方未提供）' : ''}
                        </option>
                        <option value="qr">二维码登录</option>
                      </Choice>
                    </label>
                    {form.platform === 'wechat' && (
                      <p className="form-hint">微信小店官方目前只提供扫码登录，账号密码选项不可用。</p>
                    )}
                    {form.platform === 'douyin' && form.login_method === 'password' && (
                      <p className="form-hint">抖店密码登录使用邮箱账号；手机号登录需要在官方画面接收短信验证码。</p>
                    )}
                    {form.login_method === 'password' && (
                      <p className="form-hint">验证码请在浏览器中完成；无密码入口时请切换扫码登录。</p>
                    )}
                    {form.login_method === 'qr' && (
                      <p className="form-hint">
                        保存后使用对应平台 App 扫码登录。
                        {form.platform === 'taofactory'
                          ? '淘工厂通过淘宝 / 千牛扫码后返回官方工作台，可能仍需账号验证。'
                          : ''}
                      </p>
                    )}
                    <label>
                      {form.login_method === 'qr' ? '账号备注（选填）' : '账号'}
                      <TextInput
                        required={form.login_method === 'password'}
                        autoComplete="off"
                        value={form.username}
                        maxLength={256}
                        onChange={(e) => setForm({ ...form, username: e.target.value })}
                      />
                    </label>
                    {form.login_method === 'password' && (
                      <label>
                        {editing ? '新密码（留空保持原密码）' : '密码'}
                        <TextInput
                          required={!editing}
                          type="password"
                          autoComplete="new-password"
                          value={form.password}
                          maxLength={1024}
                          onChange={(e) => setForm({ ...form, password: e.target.value })}
                        />
                      </label>
                    )}
                    <label>
                      浏览器
                      <Choice value={form.browser} onChange={(e) => setForm({ ...form, browser: e.target.value })}>
                        {['system', 'bundled'].map((kind) => (
                          <option
                            key={kind}
                            value={kind}
                            disabled={
                              !(centerMode && !editing) && browsers.find((b) => b.id === kind)?.available === false
                            }
                          >
                            {kind === 'system' ? '系统 Google Chrome' : '项目自带 Google Chrome'}
                            {!(centerMode && !editing) && browsers.find((b) => b.id === kind)?.available === false
                              ? '（未安装）'
                              : ''}
                          </option>
                        ))}
                      </Choice>
                    </label>
                    {editing && (
                      <p className="form-hint">
                        修改账号、密码或浏览器会关闭当前浏览器。账号或密码变更后，需要重新登录。
                      </p>
                    )}
                    <NativeButton className="primary" type="submit" disabled={saving}>
                      {saving ? '正在保存…' : editing ? '保存修改' : '添加店铺'}
                    </NativeButton>
                  </form>
                </AdminDialog>
              )}
              {deleting && (
                <AdminDialog>
                  <section className="admin-dialog-content" aria-label="删除店铺">
                    <div className="dialog-head">
                      <h2>删除店铺</h2>
                      <NativeButton
                        disabled={saving}
                        aria-label="关闭"
                        data-dialog-close
                        onClick={() => setDeleting(null)}
                      >
                        ×
                      </NativeButton>
                    </div>
                    <p>确定删除「{shopLabel(deleting)}」？</p>
                    <p className="account">{deleting.username}</p>
                    <p>将关闭该店铺浏览器，移除数据库中的店铺配置和保存的密码。本机浏览器资料目录会保留。</p>
                    {formError && (
                      <p className="error" role="alert">
                        {formError}
                      </p>
                    )}
                    <div className="delete-actions">
                      <NativeButton disabled={saving} onClick={() => setDeleting(null)}>
                        取消
                      </NativeButton>
                      <NativeButton className="danger" disabled={saving} onClick={deleteShop}>
                        {saving ? '正在删除…' : '确认删除'}
                      </NativeButton>
                    </div>
                  </section>
                </AdminDialog>
              )}
              {connected && <ProductOverview onNavigate={navigate} />}
              {selected && <Viewer shop={selected} onClose={() => setViewer(null)} onError={setMessage} />}
            </main>
          </HomeTheme>
        </AdminWorkspaceContext.Provider>
      </ShopOperatorsProvider>
    </WorkspaceProvider>
  );
}
