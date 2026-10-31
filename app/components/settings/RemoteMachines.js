'use client';
import { Choice } from '../shared/AdminControls';
import { clearSnapshots } from '../../lib/response-snapshots.mjs';
import { useEffect, useState } from 'react';
import CenterMachines from './CenterMachines';
import { fetchJson } from '../../lib/fetch-json';
export async function nodeApi(path, body, method = body === undefined ? 'GET' : 'POST', signal) {
  if (method !== 'GET') clearSnapshots();
  return fetchJson('/api/' + path, {
    method,
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(20000)]) : AbortSignal.timeout(20000),
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}
export function MachinePicker() {
  const [nodes, setNodes] = useState([]),
    [active, setActive] = useState(0),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    let alive = true;
    Promise.all([nodeApi('nodes'), nodeApi('active-node')])
      .then(([n, a]) => {
        if (alive) {
          setNodes(n);
          setActive(a.id);
        }
      })
      .catch((e) => {
        if (alive) setError('机器列表读取失败：' + e.message);
      });
    return () => {
      alive = false;
    };
  }, []);
  async function choose(id) {
    if (!window.dispatchEvent(new Event('workspace-before-navigate', { cancelable: true }))) return;
    setBusy(true);
    try {
      await nodeApi('active-node', { id: Number(id) });
      window.location.reload();
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  }
  return (
    <div className="machine-picker">
      <label>
        工作机器
        <Choice aria-label="工作机器" value={active} disabled={busy} onChange={(e) => choose(e.target.value)}>
          <option value={0}>{nodes.some((n) => n.center) ? '全部机器' : '主服务'}</option>
          {nodes.map((n) => (
            <option key={n.id} value={n.id}>
              {n.name}
            </option>
          ))}
          {active > 0 && !nodes.some((n) => n.id === active) && (
            <option value={active}>机器不可用 · 可切回主服务</option>
          )}
        </Choice>
      </label>
      {error && (
        <span role="alert" className="error">
          {error}
        </span>
      )}
    </div>
  );
}
export default function RemoteMachines({ Viewer, user }) {
  return <CenterMachines Viewer={Viewer} user={user} />;
}
