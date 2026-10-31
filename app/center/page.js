'use client';
import { NativeButton, TextInput, Choice } from '../components/shared/AdminControls';
import SortableTable from '../components/shared/SortableTable';
import AdminDialog from '../components/shared/AdminDialog';
import { useState, useEffect, useCallback, useRef } from 'react';
import { fetchJson } from '../lib/fetch-json';
import NodeManager, { centerApi } from '../components/collection/NodeCard';
import { moduleNames } from '../components/shell/WorkspaceTools';
import { shopLabel } from '../lib/shop-identity.mjs';
import './center.css';
import LoginPanel from '../components/auth/LoginPanel';
import LanguageSwitcher from '../i18n/LanguageSwitcher';

const jobKinds = ['overview', 'orders', 'products', 'reviews', 'invoices', 'packages', 'aftersales'];
const jobStates = { queued: '等待节点领取', leased: '派发中', dispatched: '已加入节点队列', failed: '派发失败' };
const scopeNames = { ...moduleNames, collector_status: '采集机状态' };
const SCOPE_LIMIT = 500;
const time = (value) => (value ? new Date(value * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '—');

function Value({ value, depth = 0 }) {
  if (value == null || value === '') return <span className="center-muted">—</span>;
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value !== 'object') return String(value);
  if (Array.isArray(value)) {
    if (!value.length) return <span className="center-muted">空列表</span>;
    if (depth > 1 || value.some((v) => !v || typeof v !== 'object' || Array.isArray(v)))
      return (
        <span>
          {value
            .slice(0, 20)
            .map((v) => (typeof v === 'object' ? JSON.stringify(v) : String(v)))
            .join('、')}
          {value.length > 20 ? ` 等 ${value.length} 项` : ''}
        </span>
      );
    const columns = [...new Set(value.slice(0, 20).flatMap(Object.keys))].slice(0, 10);
    return (
      <div className="center-table-wrap">
        <SortableTable className="center-table">
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c}>{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {value.slice(0, 50).map((row, i) => (
              <tr key={i}>
                {columns.map((c) => (
                  <td key={c}>
                    {row[c] !== null && typeof row[c] === 'object' ? (
                      JSON.stringify(row[c]).slice(0, 120)
                    ) : (
                      <Value value={row[c]} depth={depth + 1} />
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </SortableTable>
        {value.length > 50 && <p className="center-muted">仅显示前 50 条，共 {value.length} 条。</p>}
      </div>
    );
  }
  const entries = Object.entries(value);
  if (!entries.length) return <span className="center-muted">无内容</span>;
  if (depth > 2) return <code>{JSON.stringify(value).slice(0, 200)}</code>;
  return (
    <dl className="center-fields">
      {entries.map(([k, v]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>
            <Value value={v} depth={depth + 1} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

export default function Center() {
  const [status, setStatus] = useState('checking'),
    [error, setError] = useState('');
  const [nodes, setNodes] = useState([]),
    [scopes, setScopes] = useState([]),
    [jobs, setJobs] = useState([]),
    [shops, setShops] = useState(null),
    [detail, setDetail] = useState(null);
  const [jobNode, setJobNode] = useState(''),
    [jobShop, setJobShop] = useState(''),
    [jobKind, setJobKind] = useState('overview');
  const reading = useRef(false),
    scopesReadAt = useRef(0),
    ready = status === 'ready';
  const signedOut = useCallback(() => {
    setStatus('login');
    setNodes([]);
    setScopes([]);
    setJobs([]);
    setShops(null);
    setDetail(null);
  }, []);
  const refresh = useCallback(
    async (force = false) => {
      if (reading.current || (!force && document.hidden)) return;
      reading.current = true;
      try {
        // Stored-scope metadata changes slowly; read it at most once a minute unless forced.
        const readScopes = force || Date.now() - scopesReadAt.current > 60000;
        const [n, s, j] = await Promise.all([
          centerApi('nodes'),
          readScopes ? centerApi(`snapshots?limit=${SCOPE_LIMIT}`) : null,
          centerApi('jobs'),
        ]);
        setNodes(n.map((node) => ({ ...node, node: node.node || node.id })));
        if (s) {
          scopesReadAt.current = Date.now();
          setScopes(s);
        }
        setJobs(j);
        setStatus('ready');
        setError('');
        fetchJson('/api/shops')
          .then(setShops)
          .catch(() => setShops((current) => current || null));
      } catch (e) {
        if (e.status === 401 || e.status === 403) signedOut();
        else {
          setError(e.message);
          setStatus((current) => (current === 'checking' ? 'login' : current));
        }
      } finally {
        reading.current = false;
      }
    },
    [signedOut],
  );
  useEffect(() => {
    refresh(true);
  }, [refresh]);
  useEffect(() => {
    if (!ready) return;
    const id = setInterval(refresh, 10000);
    const visible = () => {
      if (!document.hidden) refresh();
    };
    document.addEventListener('visibilitychange', visible);
    return () => {
      clearInterval(id);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [ready, refresh]);
  async function action(callback) {
    try {
      await callback();
      setError('');
    } catch (e) {
      if (e.status === 401 && ready) signedOut();
      else setError(e.message);
    }
  }
  const nodeShops = shops?.filter((s) => s.node_id === jobNode) || [];
  const shopName = (node, local) => {
    const s = shops?.find((x) => x.node_id === node && String(x.local_id) === String(local));
    return s ? shopLabel(s) : String(local) === '0' ? '节点自身' : `店铺 ${local}`;
  };
  useEffect(() => {
    if (!jobNode && nodes.length) setJobNode(nodes[0].node);
  }, [nodes, jobNode]);
  useEffect(() => {
    setJobShop('');
  }, [jobNode]);
  return (
    <main className={`center-page${ready ? '' : ' center-landing'}`}>
      {!ready ? (
        <LoginPanel onReady={() => location.replace('/')} />
      ) : (
        <>
          <h1>多店铺数据工作台 · 中心服务</h1>
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          <div className="center-toolbar">
            <a className="center-link-button" href="/">
              进入完整工作台
            </a>
            <NativeButton onClick={() => refresh(true)}>刷新数据库数据</NativeButton>
            <LanguageSwitcher />
            <NativeButton
              onClick={() =>
                action(async () => {
                  await fetchJson('/api/auth/logout', { method: 'POST' });
                  setError('');
                  signedOut();
                })
              }
            >
              退出
            </NativeButton>
          </div>
          <h2>采集节点</h2>
          <NodeManager nodes={nodes} onChanged={() => refresh(true)} onError={setError} />
          <h2>中心已存数据</h2>
          <p className="center-muted">页面只读中心数据库。采集机保持现有采集频率，发布成功的数据通过主动连接回传。</p>
          <div className="center-table-wrap center-scroll">
            <SortableTable className="center-table">
              <thead>
                <tr>
                  {['节点', '店铺', '数据范围', '最后更新', '操作'].map((h) => (
                    <th key={h}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {scopes.map((s) => (
                  <tr key={`${s.node}/${s.shop}/${s.scope}`}>
                    <td>{nodes.find((n) => n.node === s.node)?.name || '已移除节点'}</td>
                    <td>{shopName(s.node, s.shop)}</td>
                    <td>{scopeNames[s.scope] || s.scope}</td>
                    <td>{time(s.updated)}</td>
                    <td>
                      <NativeButton
                        onClick={() =>
                          action(async () =>
                            setDetail({
                              ...s,
                              rows: await centerApi(
                                'data?' + new URLSearchParams({ node: s.node, shop: s.shop, scope: s.scope }),
                              ),
                            }),
                          )
                        }
                      >
                        查看
                      </NativeButton>
                    </td>
                  </tr>
                ))}
              </tbody>
            </SortableTable>
            {!scopes.length && <p className="center-muted">中心数据库暂无已回传数据。</p>}
            {scopes.length >= SCOPE_LIMIT && (
              <p className="center-muted">仅显示最近更新的 {SCOPE_LIMIT} 条数据范围。</p>
            )}
          </div>
          <h2>立即采集</h2>
          <form
            className="center-job"
            onSubmit={(e) => {
              e.preventDefault();
              action(async () => {
                await centerApi('jobs', { node: jobNode, shop_id: Number(jobShop), kind: jobKind });
                await refresh(true);
              });
            }}
          >
            <label>
              采集节点
              <Choice required value={jobNode} onChange={(e) => setJobNode(e.target.value)}>
                {nodes.map((n) => (
                  <option value={n.node} key={n.node}>
                    {n.name}
                  </option>
                ))}
              </Choice>
            </label>
            <label>
              店铺
              {shops ? (
                <Choice
                  required
                  value={jobShop}
                  onChange={(e) => setJobShop(e.target.value)}
                  disabled={!nodeShops.length}
                >
                  <option value="">{nodeShops.length ? '请选择店铺' : '该节点暂无店铺'}</option>
                  {nodeShops.map((s) => (
                    <option key={s.id} value={s.local_id}>
                      {shopLabel(s)}
                    </option>
                  ))}
                </Choice>
              ) : (
                <TextInput
                  type="number"
                  min="1"
                  placeholder="店铺列表暂不可用，请输入节点本机店铺 ID"
                  required
                  value={jobShop}
                  onChange={(e) => setJobShop(e.target.value)}
                />
              )}
            </label>
            <label>
              模块
              <Choice value={jobKind} onChange={(e) => setJobKind(e.target.value)}>
                {jobKinds.map((v) => (
                  <option value={v} key={v}>
                    {moduleNames[v] || v}
                  </option>
                ))}
              </Choice>
            </label>
            <NativeButton className="primary" disabled={!jobNode || !jobShop}>
              加入采集队列
            </NativeButton>
          </form>
          <ul className="center-jobs">
            {jobs.slice(0, 30).map((j) => (
              <li key={j.id}>
                <strong>{moduleNames[j.request?.kind] || j.request?.kind}</strong>
                <span>
                  {nodes.find((n) => n.node === j.node)?.name || '已移除节点'} · {shopName(j.node, j.request?.shop_id)}
                </span>
                <span className={j.state === 'failed' ? 'error' : ''}>
                  {jobStates[j.state] || j.state}
                  {j.result?.message ? ' · ' + j.result.message : ''}
                </span>
                <small>{time(j.created)}</small>
              </li>
            ))}
            {!jobs.length && <li className="center-muted">暂无采集任务。</li>}
          </ul>
        </>
      )}
      {detail && (
        <AdminDialog label="中心数据详情">
          <section className="admin-dialog-content center-detail" aria-labelledby="center-detail-title">
            <div className="dialog-head">
              <div>
                <h3 id="center-detail-title">{scopeNames[detail.scope] || detail.scope}</h3>
                <p>
                  {nodes.find((n) => n.node === detail.node)?.name || '已移除节点'} ·{' '}
                  {shopName(detail.node, detail.shop)} · 更新于 {time(detail.updated)}
                </p>
              </div>
              <NativeButton data-dialog-close aria-label="关闭" onClick={() => setDetail(null)}>
                ×
              </NativeButton>
            </div>
            {detail.rows.length ? (
              detail.rows.map((row, i) => (
                <section key={i} className="center-detail-row">
                  {detail.rows.length > 1 && <h4>记录 {i + 1}</h4>}
                  <Value value={row.payload ?? row} />
                </section>
              ))
            ) : (
              <p className="center-muted">没有找到数据。</p>
            )}
            <details className="center-raw">
              <summary>查看原始 JSON</summary>
              <pre>{JSON.stringify(detail.rows, null, 2)}</pre>
            </details>
          </section>
        </AdminDialog>
      )}
    </main>
  );
}
