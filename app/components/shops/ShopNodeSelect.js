'use client';
import { Choice } from '../shared/AdminControls';
import { useEffect, useState } from 'react';

export default function ShopNodeSelect({ value, onChange, disabled = false, exclude = '', onMode }) {
  const [nodes, setNodes] = useState([]),
    [center, setCenter] = useState(false),
    [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    (async () => {
      const config = await fetch('/api/connection').then((r) => r.json());
      if (!alive) return;
      setCenter(!!config.center);
      onMode?.(!!config.center);
      if (!config.center) return;
      const responses = await Promise.all([fetch('/api/nodes'), fetch('/api/active-node')]);
      if (responses.some((r) => !r.ok)) throw Error('无法读取采集节点，请关闭后重试');
      const [list, active] = await Promise.all(responses.map((r) => r.json()));
      if (!alive) return;
      setNodes(list);
      const available = list.filter((n) => n.node !== exclude);
      if (!value) {
        const initial = available.find((n) => n.id === active.id) || (available.length === 1 ? available[0] : null);
        if (initial) onChange(String(initial.id));
      }
    })().catch((e) => {
      if (alive) setError(e.message);
    });
    return () => {
      alive = false;
    };
  }, []);
  if (!center && !error) return null;
  return (
    <label>
      所属采集节点
      <Choice
        aria-label="所属采集节点"
        value={value || ''}
        disabled={disabled}
        required
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">请选择采集节点</option>
        {nodes
          .filter((n) => n.node !== exclude)
          .map((n) => (
            <option key={n.id} value={n.id} disabled={!n.online}>
              {n.name}
              {!n.online ? '（离线）' : ''}
            </option>
          ))}
      </Choice>
      {error && (
        <span role="alert" className="error">
          {error}
        </span>
      )}
    </label>
  );
}
