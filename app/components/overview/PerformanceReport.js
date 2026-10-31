'use client';
import SortableTable from '../shared/SortableTable';
import { useEffect, useState } from 'react';
import { performanceSummary } from '../../lib/ui-performance.mjs';
export default function PerformanceReport() {
  const [data, setData] = useState(null),
    [server, setServer] = useState(null);
  useEffect(() => {
    const c = new AbortController();
    fetch('/api/system/performance', { signal: c.signal })
      .then((r) => (r.ok ? r.json() : null))
      .then(setServer)
      .catch(() => {});
    const load = () => {
      if (!document.hidden) setData(performanceSummary());
    };
    load();
    const t = setInterval(load, 5000);
    return () => {
      clearInterval(t);
      c.abort();
    };
  }, []);
  return (
    <details className="settings-card performance-report">
      <summary>本页面性能诊断</summary>
      <p>仅记录当前标签页的接口路径与耗时；不记录账号、搜索词或业务数据。P95 为最近 100 次样本。</p>
      <p>
        浏览器长任务 {data?.longTasks || 0} 次 · 合计 {data?.longMs || 0} ms
      </p>
      <div className="data-table-wrap">
        <SortableTable>
          <thead>
            <tr>
              <th>模块接口</th>
              <th>请求数</th>
              <th>最近耗时</th>
              <th>P95</th>
              <th>缓存命中</th>
            </tr>
          </thead>
          <tbody>
            {data?.rows.map((r) => (
              <tr key={r.path}>
                <td>{r.path}</td>
                <td>{r.count}</td>
                <td>{r.last} ms</td>
                <td>{r.p95} ms</td>
                <td>
                  {r.hits}/{r.count}
                </td>
              </tr>
            ))}
          </tbody>
        </SortableTable>
      </div>
      {server && (
        <details>
          <summary>数据库服务耗时（本次打开时）</summary>
          {server.rows.map((r) => (
            <p key={r.path}>
              {r.path} · P95 {r.p95} ms · 缓存命中 {r.hits}/{r.count}
            </p>
          ))}
        </details>
      )}
    </details>
  );
}
