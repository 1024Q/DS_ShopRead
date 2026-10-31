'use client';
import { useEffect, useRef, useState } from 'react';
import { shopLabel } from '../../lib/shop-identity.mjs';
import { fetchJson, shopStateLabels } from '../../lib/fetch-json';
import NodeManager, { nodeOnline } from '../collection/NodeCard';
import permissions from '../../../lib/ui-permissions.cjs';

export default function CenterMachines({ Viewer, user }) {
  const [nodes, setNodes] = useState(null),
    [shops, setShops] = useState([]),
    [loadError, setLoadError] = useState(''),
    [error, setError] = useState(''),
    [viewer, setViewer] = useState(null);
  const reading = useRef(false);
  async function load() {
    if (reading.current || document.hidden) return;
    reading.current = true;
    try {
      const [a, b] = await Promise.all([fetchJson('/api/nodes'), fetchJson('/api/shops')]);
      setNodes(a);
      setShops(b);
      setLoadError('');
    } catch (e) {
      setLoadError(e.message);
    } finally {
      reading.current = false;
    }
  }
  useEffect(() => {
    load();
    const t = setInterval(load, 10000);
    const visible = () => {
      if (!document.hidden) load();
    };
    document.addEventListener('visibilitychange', visible);
    return () => {
      clearInterval(t);
      document.removeEventListener('visibilitychange', visible);
    };
  }, []);
  const details = (n) =>
    shops
      .filter((s) => s.node_id === n.node)
      .map((s) => (
        <div className="remote-shop" key={s.id}>
          <div>
            <strong>{shopLabel(s)}</strong>
            <small>{shopStateLabels[s.state] || s.state || '未知状态'}</small>
          </div>
          <button
            disabled={!nodeOnline(n) || !['shops', 'browser_workspace'].some((p) => permissions.can(user, p, true))}
            onClick={() => setViewer(s)}
          >
            远程控制
          </button>
        </div>
      ));
  return (
    <section className="business-panel">
      <div className="business-heading">
        <h2>采集机器</h2>
        <a href="/center">中心连接配置</a>
      </div>
      {loadError && (
        <p role="alert" className="error">
          {loadError}
          {nodes ? '，暂保留上次列表。' : ''}
        </p>
      )}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {nodes && user?.role !== 'admin' ? (
        nodes.map((node) => (
          <section className="settings-card" key={node.node || node.id}>
            <h3>{node.name}</h3>
            <p>{nodeOnline(node) ? '在线' : '离线'} · 机器审批和管理操作仅管理员可用</p>
            {details(node)}
          </section>
        ))
      ) : nodes ? (
        <NodeManager nodes={nodes} onChanged={load} onError={setError} renderDetails={details} />
      ) : (
        !loadError && <p role="status">正在读取采集机器…</p>
      )}
      {viewer && <Viewer shop={viewer} onClose={() => setViewer(null)} onError={setError} />}
    </section>
  );
}
