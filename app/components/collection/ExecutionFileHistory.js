'use client';
import { useEffect, useState } from 'react';
import { Alert, Button, Select, Space, Table, Typography } from 'antd';
import { moduleNames } from '../shell/WorkspaceTools';
import { executionLogRows } from '../../lib/collection-management.mjs';
import { beijingDay } from '../../lib/beijing-day.mjs';

const stamp = (v) =>
  v ? new Date(v * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—';
export default function ExecutionFileHistory({ shopId, kinds }) {
  const [period, setPeriod] = useState('today');
  const [module, setModule] = useState(''),
    [version, setVersion] = useState(0),
    [cursor, setCursor] = useState(null);
  const [data, setData] = useState({ rows: [] }),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setBusy(true);
    setError('');
    const query = new URLSearchParams({
      shop_id: String(shopId),
      module,
      limit: '50',
      ...(period === 'today' ? { date_from: beijingDay() } : {}),
      ...(cursor || {}),
    });
    fetch('/api/logs?' + query, { signal: controller.signal, cache: 'no-store' })
      .then(async (r) => {
        if (!r.ok)
          throw Error(
            r.status === 403
              ? '当前账号没有日志查看权限，已保留上方可查看的执行状态。'
              : '文件日志暂不可用，请稍后刷新。',
          );
        const value = await r.json();
        if (active) setData({ ...value, rows: value.rows || [] });
      })
      .catch((e) => {
        if (active && e.name !== 'AbortError') setError(e.message);
      })
      .finally(() => {
        if (active) setBusy(false);
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [shopId, module, version, cursor, period]);
  return (
    <Space orientation="vertical" style={{ width: '100%' }} size="middle">
      <Typography.Title level={5}>文件执行记录</Typography.Title>
      <Typography.Text type="secondary">
        保留原始文件日志；恢复后仍可查看历史失败时间。默认读取当天最近50条，可切换近7天；长日志使用独立入口。
      </Typography.Text>
      <Space wrap>
        <Select
          aria-label="执行记录时间范围"
          value={period}
          style={{ width: 140 }}
          options={[
            { value: 'today', label: '当天记录' },
            { value: 'week', label: '最近7天' },
          ]}
          onChange={(v) => {
            setPeriod(v);
            setCursor(null);
          }}
        />
        <Select
          aria-label="执行记录模块筛选"
          allowClear
          value={module || undefined}
          placeholder="全部模块"
          style={{ width: 180 }}
          options={[...new Set(kinds)].map((value) => ({ value, label: moduleNames[value] || value }))}
          onChange={(v) => {
            setModule(v || '');
            setCursor(null);
          }}
        />
        <Button
          loading={busy}
          onClick={() => {
            setCursor(null);
            setVersion((v) => v + 1);
          }}
        >
          刷新文件记录
        </Button>
        <Button disabled={busy || !data.next} onClick={() => setCursor(data.next)}>
          更早记录
        </Button>
      </Space>
      {error && <Alert type="warning" showIcon title={error} />}
      <Table
        size="small"
        loading={busy}
        rowKey={(r) => `${r.id}:${r.first_seen}`}
        dataSource={executionLogRows(data.rows)}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        scroll={{ x: 900 }}
        columns={[
          { title: '首次发生', dataIndex: 'first_seen', width: 180, render: stamp },
          { title: '最近发生', dataIndex: 'last_seen', width: 180, render: stamp },
          { title: '次数', dataIndex: 'count', width: 75, render: (v) => v || 1 },
          { title: '模块', dataIndex: 'module', width: 110, render: (v) => moduleNames[v] || v },
          {
            title: '结果 / 原因',
            dataIndex: 'message',
            width: 340,
            render: (v, r) => <Typography.Text type={r.level === 'error' ? 'danger' : undefined}>{v}</Typography.Text>,
          },
        ]}
      />
    </Space>
  );
}
