'use client';
import SortableTable from '../shared/SortableTable';
import { editorGroups, editorSteps, effectiveStepMode, formatCadence, stepParent } from '../../lib/task-layout.mjs';
const fields = {
  enabled: '自动采集',
  keepalive_enabled: '登录维护',
  keepalive_seconds: '登录维护间隔（秒）',
  batch_pages: '每批页数',
  batch_seconds: '每批秒数',
  max_concurrent: '节点总并发',
  shop_concurrent: '默认单店并发',
  shop_limits: '单店覆盖',
  max_active_shops: '同时运行店铺数',
  browser_idle_seconds: '浏览器空闲休眠（秒）',
};
export default function NodeConfigDifferences({ nodes = [], platform }) {
  if (nodes.length < 2) return null;
  const available = nodes.filter((n) => n.collection),
    missing = nodes.filter((n) => n.error || !n.online);
  const ids = [...new Set(available.flatMap((n) => (n.collection.frequency_groups || []).map((g) => g.id)))];
  const rows = [
    ...ids.map((id) => [
      id,
      (c) => {
        const g = c.frequency_groups?.find((g) => g.id === id);
        return g ? `${g.name} · ${formatCadence(g)} · 超时 ${g.timeout} 秒` : '无此分组';
      },
    ]),
    ...Object.entries(fields).map(([key, name]) => [
      name,
      (c) =>
        typeof c[key] === 'boolean'
          ? c[key]
            ? '开启'
            : '关闭'
          : typeof c[key] === 'object'
            ? JSON.stringify(c[key])
            : String(c[key] ?? '—'),
    ]),
  ];
  for (const group of editorGroups({}, available)) {
    rows.push([
      `任务定义 · ${group.id}`,
      (c) => c.task_layout?.groups?.find((entry) => entry.id === group.id)?.name || '无此任务',
    ]);
  }
  if (platform)
    for (const step of editorSteps({}, available, platform)) {
      rows.push([
        `${step.label} · 所属任务`,
        (c) => {
          const id = stepParent(c, platform, step.id);
          return c.task_layout?.groups?.find((group) => group.id === id)?.name || '未分配';
        },
      ]);
      rows.push([
        `${step.label} · 更新频率`,
        (c) => {
          if (!c.task_step_catalog?.some((entry) => entry.id === step.id && entry.platforms?.includes(platform)))
            return '未接入';
          const id = effectiveStepMode(c, platform, step);
          const group = c.frequency_groups?.find((entry) => entry.id === id);
          return id === 'off' ? '暂停采集' : group ? `${group.name} · ${formatCadence(group)}` : '未设置';
        },
      ]);
    }
  const differences = rows.filter(([, read]) => new Set(available.map((n) => read(n.collection))).size > 1);
  return (
    <div className="node-config-differences">
      <p>
        应用范围：全部 {nodes.length} 台机器 · {differences.length} 项配置差异。只下发本次修改项。
      </p>
      {missing.length > 0 && (
        <p className="error">
          {missing.map((n) => n.name + '：' + (n.error || '离线，显示上次配置')).join('；')}
          。离线节点保存可能失败，恢复后需重试。
        </p>
      )}
      <details>
        <summary>查看各机器配置差异</summary>
        {differences.length ? (
          <div style={{ overflowX: 'auto' }}>
            <SortableTable>
              <thead>
                <tr>
                  <th>配置项</th>
                  {available.map((n) => (
                    <th key={n.id}>
                      {n.name}
                      {!n.online ? '（离线）' : ''}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {differences.map(([name, read]) => (
                  <tr key={name}>
                    <td>{name}</td>
                    {available.map((n) => (
                      <td key={n.id}>{read(n.collection)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </SortableTable>
          </div>
        ) : (
          <p>已读取节点的配置一致。</p>
        )}
      </details>
    </div>
  );
}
