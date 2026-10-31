'use client';
import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Modal, Space, Table, Tag } from 'antd';
import UpdatedAgo from '../shared/UpdatedAgo';

const lists = [
  ['orders', '待发货'],
  ['aftersales', '待商家处理'],
  ['tickets', '待处理'],
];
const names = { orders: '待发货订单', aftersales: '待商家处理售后', tickets: '待处理工单' };
const labels = {
  waiting_node: '等待节点上线',
  waiting_upgrade: '等待节点升级',
  queued: '已排队',
  running: '正在补齐',
  waiting_login: '等待登录验证',
  retry: '等待重试',
  partial: '断点已保存',
  uploading: '等待中心确认',
  completed: '中心已确认',
};

export default function ListCompletionPanel({ shopIds = [] }) {
  const [open, setOpen] = useState(false),
    [reports, setReports] = useState({}),
    [busy, setBusy] = useState(''),
    [error, setError] = useState('');
  const refresh = useRef(null),
    ids = Array.from(new Set(shopIds.map(Number))).filter(Number.isFinite);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    let loading = false,
      active = true;
    const load = async () => {
      if (loading || document.hidden) return;
      loading = true;
      try {
        const results = await Promise.allSettled(
          lists.map(async ([kind]) => {
            const response = await fetch(`/api/data/${kind}/complete-once`, {
              cache: 'no-store',
              signal: controller.signal,
            });
            const value = await response.json();
            if (!response.ok) throw Error(typeof value.detail === 'string' ? value.detail : '补齐进度暂不可用');
            return [kind, value];
          }),
        );
        if (active) {
          setReports((old) => ({
            ...old,
            ...Object.fromEntries(results.filter((r) => r.status === 'fulfilled').map((r) => r.value)),
          }));
          const failed = results.find((r) => r.status === 'rejected' && r.reason.name !== 'AbortError');
          setError(failed ? failed.reason.message : '');
        }
      } finally {
        loading = false;
      }
    };
    refresh.current = load;
    load();
    const timer = setInterval(load, 15000);
    return () => {
      active = false;
      controller.abort();
      clearInterval(timer);
      refresh.current = null;
    };
  }, [open]);
  const submit = async (kind, status) => {
    setBusy(kind);
    setError('');
    try {
      const response = await fetch(`/api/data/${kind}/complete-once`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request_id: crypto.randomUUID(), shop_ids: ids, status }),
      });
      const value = await response.json();
      if (!response.ok) throw Error(typeof value.detail === 'string' ? value.detail : '提交失败，请稍后重试');
      setReports((old) => ({ ...old, [kind]: value }));
      refresh.current?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy('');
    }
  };
  const tasks = lists.flatMap(([kind]) =>
    (reports[kind]?.tasks || [])
      .filter((task) => ids.includes(task.shop_id))
      .map((task) => ({ ...task, kind, _key: `${kind}:${task.job_id || task.shop_id}` })),
  );
  const columns = [
    {
      title: '店铺 / 节点',
      dataIndex: 'shop_name',
      render: (name, row) => (
        <>
          <strong>{name || `店铺 ${row.shop_id}`}</strong>
          <small style={{ display: 'block' }}>{row.machine_name || '—'}</small>
        </>
      ),
    },
    { title: '列表', dataIndex: 'kind', render: (kind) => names[kind] },
    {
      title: '状态 / 进度',
      dataIndex: 'status',
      render: (status, row) => (
        <>
          <Tag color={status === 'completed' ? 'green' : status === 'retry' ? 'orange' : 'blue'}>
            {labels[status] || status}
          </Tag>
          <div>{row.stage || ''}</div>
          {row.loaded != null && (
            <small>
              {row.loaded} / {row.total ?? '—'} 条
            </small>
          )}
          {row.last_error && <div role="status">{row.last_error}</div>}
        </>
      ),
    },
    {
      title: '中心确认',
      dataIndex: 'center_confirmed',
      render: (confirmed, row) =>
        confirmed ? (
          <UpdatedAgo value={row.center_received_at || row.completed_at} />
        ) : row.status === 'completed' ? (
          '待核对'
        ) : (
          '尚未确认'
        ),
    },
  ];
  return (
    <>
      <Button size="small" onClick={() => setOpen(true)}>
        一次性补齐列表
      </Button>
      <Modal
        title="一次性补齐列表"
        open={open}
        onCancel={() => setOpen(false)}
        footer={<Button onClick={() => setOpen(false)}>关闭</Button>}
        width={980}
      >
        <p>
          提交范围：当前筛选的 {ids.length}{' '}
          家拼多多店铺。只补齐所选列表，沿用现有并发与休眠设置，已停用的定时任务保持停用。离线节点上线并更新后继续执行。
        </p>
        <Space wrap style={{ marginBottom: 16 }}>
          {lists.map(([kind, status]) => (
            <Button
              key={kind}
              disabled={!ids.length || (!!busy && busy !== kind)}
              loading={busy === kind}
              onClick={() => submit(kind, status)}
            >
              补齐{names[kind]}
            </Button>
          ))}
          <Button onClick={() => refresh.current?.()}>刷新进度</Button>
        </Space>
        {error && <Alert type="warning" showIcon title={error} style={{ marginBottom: 12 }} />}
        <Table
          rowKey="_key"
          size="small"
          columns={columns}
          dataSource={tasks}
          scroll={{ x: 760 }}
          pagination={{ pageSize: 10, showSizeChanger: false }}
          locale={{ emptyText: '尚未提交一次性补齐任务' }}
        />
        <p className="data-note">
          以下显示各列表最近提交的进度。“中心已确认”表示完整结果已上传；补齐完成后不会自动转为定时采集。
        </p>
      </Modal>
    </>
  );
}
