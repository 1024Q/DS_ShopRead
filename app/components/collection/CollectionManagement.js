'use client';
import { useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd';
import { DownloadOutlined, ReloadOutlined, SettingOutlined } from '@ant-design/icons';
import { useSyncStatus } from '../../providers/WorkspaceContext';
import { shopRows, orderedIds, runLabels, stepState, leafSteps, csvCell } from '../../lib/collection-management.mjs';
import { shopLabel } from '../../lib/shop-identity.mjs';
import ModuleQueueBoard from './ModuleQueueBoard';
import ConcurrencySettings from './ConcurrencySettings';
import QueueDiagnostics from './QueueDiagnostics';
import ClearQueueButton from './ClearQueueButton';
import DateCollection from './DateCollection';
import TaskTiming from './TaskTiming';
import ExecutionFileHistory from './ExecutionFileHistory';

const stamp = (v) =>
  v ? new Date(v * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—';
const color = { running: 'processing', waiting: 'default', issue: 'error' };
function Upload({ value, detail = false }) {
  const names = {
    confirmed: '中心已确认',
    pending: '本地已保存 · 等待上传',
    failed: '本地已保存 · 上传失败',
    unpublished: '尚无上传记录',
  };
  return (
    <Space orientation="vertical" size={0}>
      <Tag color={value.state === 'failed' ? 'error' : value.state === 'confirmed' ? 'success' : 'default'}>
        {names[value.state]}
      </Tag>
      {value.pending > 0 && <Typography.Text type="secondary">{value.pending} 项待确认</Typography.Text>}
      {detail && value.oldest && <span>最早待确认：{stamp(value.oldest)}</span>}
      {detail && value.error && <Typography.Text type="danger">{value.error}</Typography.Text>}
    </Space>
  );
}
export default function CollectionManagement({ shops, inventory, onOpen, onNavigate }) {
  const { status, statusError } = useSyncStatus();
  const [tab, setTab] = useState('shops'),
    [selected, setSelected] = useState(null),
    [drawerTab, setDrawerTab] = useState('steps');
  const [query, setQuery] = useState(''),
    [state, setState] = useState(null),
    [node, setNode] = useState(null);
  const [sort, setSort] = useState({ field: 'name', order: 'ascend' });
  const [columnKeys, setColumnKeys] = useState([
    'name',
    'node',
    'state',
    'current',
    'wait',
    'next',
    'upload',
    'actions',
  ]);
  const [revision, setRevision] = useState(0),
    [page, setPage] = useState(1);
  const pinned = useRef({ key: '', ids: [] });
  const rows = useMemo(() => shopRows(shops, status?.master_tasks), [shops, status?.master_tasks]);
  const filtered = rows.filter(
    (r) =>
      (!state || r.runState === state) &&
      (!node || r.nodeName === node) &&
      (!query ||
        [shopLabel(r), r.nodeName, r.current?.name, r.waitingReason]
          .join(' ')
          .toLowerCase()
          .includes(query.toLowerCase())),
  );
  const orderKey = JSON.stringify([query, state, node, sort, revision]);
  const desired = orderedIds(filtered, sort.field, sort.order);
  if (pinned.current.key !== orderKey) pinned.current = { key: orderKey, ids: desired };
  const ids = [
    ...pinned.current.ids.filter((id) => filtered.some((r) => r.key === id)),
    ...desired.filter((id) => !pinned.current.ids.includes(id)),
  ];
  const positions = new Map(ids.map((id, i) => [id, i]));
  const visible = [...filtered].sort((a, b) => positions.get(a.key) - positions.get(b.key));
  const changedOrder = desired.some((id, i) => ids[i] !== id);
  const current = rows.find((r) => r.key === selected);
  const open = (row) => {
    setSelected(row.key);
    setDrawerTab('steps');
  };
  const resetPage = (fn) => (value) => {
    fn(value);
    setPage(1);
  };
  const exportRows = () => {
    const columns = ['店铺', '节点', '运行状态', '当前任务 / 步骤', '等待原因', '下次执行', '数据上传'];
    const text = [
      columns,
      ...visible.map((r) => [
        shopLabel(r),
        r.nodeName,
        runLabels[r.runState],
        r.current?.name || '',
        r.waitingReason,
        stamp(r.nextAt),
        r.upload.state,
      ]),
    ]
      .map((r) => r.map(csvCell).join(','))
      .join('\r\n');
    const url = URL.createObjectURL(new Blob(['\ufeff' + text], { type: 'text/csv;charset=utf-8' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = '采集店铺运行.csv';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const columns = [
    {
      key: 'name',
      dataIndex: 'name',
      title: '店铺',
      width: 230,
      fixed: 'left',
      sorter: true,
      render: (_, r) => (
        <Button
          type="link"
          style={{ padding: 0, height: 'auto', whiteSpace: 'normal', textAlign: 'left' }}
          onClick={() => open(r)}
        >
          {shopLabel(r)}
        </Button>
      ),
    },
    { key: 'node', dataIndex: 'nodeName', title: '节点', width: 135, sorter: true },
    {
      key: 'state',
      dataIndex: 'runState',
      title: '运行状态',
      width: 125,
      sorter: true,
      render: (v) => <Tag color={color[v]}>{runLabels[v]}</Tag>,
    },
    {
      key: 'current',
      title: '当前任务 / 步骤',
      width: 240,
      render: (_, r) => (
        <Space orientation="vertical" size={0}>
          <span>{r.tasks.find((t) => t.steps?.some((s) => s.cache_key === r.current?.cache_key))?.module || '—'}</span>
          <Typography.Text type="secondary">
            {r.current?.name || '等待到期'}
            {r.runningCount > 1 ? ` · 另有${r.runningCount - 1}项正在执行` : ''}
          </Typography.Text>
        </Space>
      ),
    },
    { key: 'wait', dataIndex: 'waitingReason', title: '等待原因', width: 260, ellipsis: true, render: (v) => v || '—' },
    {
      key: 'next',
      dataIndex: 'nextAt',
      title: '下次执行',
      width: 185,
      sorter: true,
      render: (v, r) =>
        r.state === 'stopped' ? '已停止' : !v ? '等待计划' : v <= Date.now() / 1000 ? '已到期，等待名额' : stamp(v),
    },
    { key: 'upload', title: '数据上传', width: 195, render: (_, r) => <Upload value={r.upload} /> },
    {
      key: 'actions',
      title: '操作',
      width: 100,
      fixed: 'right',
      render: (_, r) => (
        <Button size="small" onClick={() => open(r)}>
          详情
        </Button>
      ),
    },
  ].filter((c) => columnKeys.includes(c.key));
  const steps = current ? leafSteps(current.tasks) : [];
  const stepColumns = [
    { title: '子步骤', dataIndex: 'name', width: 220 },
    { title: '状态', width: 120, render: (_, s) => <Tag color={color[stepState(s)]}>{runLabels[stepState(s)]}</Tag> },
    {
      title: '当前步骤 / 等待原因',
      dataIndex: 'stage',
      width: 260,
      render: (v, s) => (
        <Space orientation="vertical" size={0}>
          <span>{v}</span>
          {s.wait_reason_since && (
            <Typography.Text type="secondary">此原因开始：{stamp(s.wait_reason_since)}</Typography.Text>
          )}
          {s.unblock_condition && <Typography.Text type="secondary">解除条件：{s.unblock_condition}</Typography.Text>}
          {stepState(s) === 'issue' && <Typography.Text type="danger">{s.reason || s.last_error}</Typography.Text>}
        </Space>
      ),
    },
    { title: '更新频率', dataIndex: 'frequency', width: 155 },
    { title: '下次执行', width: 185, render: (_, s) => stamp(s.next_run_at || s.retry_at) },
    { title: '断点', width: 110, render: (_, s) => (s.continuation ? '已保留' : '—') },
  ];
  const runtime = (
    <Card size="small">
      <Space orientation="vertical" size="middle" style={{ display: 'flex' }}>
        {status?.dispatch?.limits && (
          <Alert
            type="info"
            showIcon
            title={
              <Space wrap>
                <span>
                  {status.dispatch.reported_nodes
                    ? `${status.dispatch.reported_nodes} 个已报告节点合计读取`
                    : '节点读取'}{' '}
                  {status.dispatch.reading} / {status.dispatch.limits.max_concurrent}
                </span>
                <span>
                  入库 {status.dispatch.database} · 等待续读 {status.dispatch.resuming} · 结果确认{' '}
                  {status.dispatch.finishing} · 浏览器准备 {status.dispatch.preparing_shops}
                </span>
              </Space>
            }
            description={
              Object.entries(status.dispatch.waiting_reasons || {})
                .map(([code, count]) => `${status.dispatch.reason_labels?.[code] || code} ${count} 项`)
                .join('；') || '没有等待需求'
            }
          />
        )}
        <Space wrap>
          <Input.Search
            aria-label="采集店铺搜索"
            placeholder="店铺 / 节点 / 任务 / 等待原因"
            allowClear
            value={query}
            onChange={(e) => resetPage(setQuery)(e.target.value)}
            style={{ width: 300, maxWidth: '100%' }}
          />
          <Select
            aria-label="采集运行状态"
            placeholder="全部运行状态"
            allowClear
            value={state}
            onChange={resetPage(setState)}
            style={{ width: 160 }}
            options={Object.entries(runLabels).map(([value, label]) => ({ value, label }))}
          />
          <Select
            aria-label="采集节点筛选"
            placeholder="全部节点"
            allowClear
            value={node}
            onChange={resetPage(setNode)}
            style={{ width: 160 }}
            options={[...new Set(rows.map((r) => r.nodeName))].map((value) => ({ value, label: value }))}
          />
        </Space>
        <Space wrap style={{ justifyContent: 'space-between', width: '100%' }}>
          <Typography.Text type="secondary">
            {visible.length} 家店铺 · 正在执行 {rows.filter((r) => r.runState === 'running').length} · 异常待处理{' '}
            {rows.filter((r) => r.runState === 'issue').length}
            {query || state || node ? ' · 已应用筛选' : ''}
          </Typography.Text>
          <Space wrap>
            <Button icon={<ReloadOutlined />} onClick={() => setRevision((v) => v + 1)}>
              {changedOrder ? '有更新，应用最新排序' : '更新排序'}
            </Button>
            <Select
              aria-label="采集表格显示列"
              mode="multiple"
              maxTagCount={0}
              maxTagPlaceholder="列设置"
              style={{ width: 145 }}
              value={columnKeys}
              onChange={(keys) => setColumnKeys([...new Set(['name', 'actions', ...keys])])}
              options={['name', 'node', 'state', 'current', 'wait', 'next', 'upload', 'actions'].map((value, i) => ({
                value,
                label: ['店铺', '节点', '运行状态', '当前任务 / 步骤', '等待原因', '下次执行', '数据上传', '操作'][i],
                disabled: ['name', 'actions'].includes(value),
              }))}
            />
            <Button icon={<DownloadOutlined />} onClick={exportRows}>
              导出筛选结果
            </Button>
          </Space>
        </Space>
        <Table
          size="middle"
          rowKey="key"
          dataSource={visible}
          columns={columns}
          scroll={{ x: 1470 }}
          loading={!status && !statusError}
          pagination={{
            current: page,
            pageSize: 20,
            onChange: setPage,
            showSizeChanger: false,
            hideOnSinglePage: true,
          }}
          onChange={(_, __, sorter) => {
            if (sorter.field && sorter.order) {
              setSort({ field: sorter.field, order: sorter.order });
              setPage(1);
            }
          }}
        />
      </Space>
    </Card>
  );
  return (
    <section className="business-panel collection-management" style={{ minWidth: 0 }}>
      <Space wrap style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
        <div>
          <h2>采集管理</h2>
          <Typography.Text type="secondary">
            每店按到期计划执行；名额不足时保留同一条需求，完成后立即补位。
          </Typography.Text>
        </div>
        <Space wrap>
          <Button onClick={() => onNavigate?.('logs')}>查看长日志</Button>
          <DateCollection shops={shops} onCollected={(kind, filters) => onNavigate?.(kind, filters)} />
        </Space>
      </Space>
      {statusError && <Alert type="warning" showIcon title="进度暂不可用，保留上次数据" description={statusError} />}
      <Tabs
        activeKey={tab}
        onChange={setTab}
        destroyOnHidden={false}
        items={[
          { key: 'shops', label: '店铺运行', children: runtime },
          {
            key: 'plans',
            label: '任务计划',
            children: (
              <Card size="small">
                <ModuleQueueBoard config={status?.collection} />
              </Card>
            ),
          },
          {
            key: 'issues',
            label: '异常与上传',
            children: (
              <>
                <Alert
                  type="info"
                  showIcon
                  title="当前问题与历史失败分别展示"
                  description="成功恢复的任务按最新状态显示；历史错误、断点及上传记录可在店铺详情中查看。中心断线时本地继续采集，确认后才标记已上传。"
                />
                <Card size="small" title="上传通道" style={{ marginTop: 16 }}>
                  <Table
                    size="small"
                    rowKey="lane"
                    pagination={false}
                    scroll={{ x: 780 }}
                    dataSource={Object.entries(status?.delivery || {}).map(([lane, row]) => ({ lane, ...row }))}
                    columns={[
                      {
                        title: '通道',
                        dataIndex: 'lane',
                        render: (v) => ({ business: '业务数据', orders: '订单增量', auxiliary: '重点待办' })[v] || v,
                      },
                      { title: '待确认', dataIndex: 'pending' },
                      { title: '最早积压时间', dataIndex: 'oldest_pending', render: stamp },
                      { title: '最近中心确认', dataIndex: 'last_success', render: stamp },
                      {
                        title: '当前失败原因',
                        dataIndex: 'error',
                        render: (v, row) => v || row.rejected_error || '无',
                      },
                    ]}
                  />
                </Card>
                <div style={{ marginTop: 16 }}>{inventory}</div>
                <QueueDiagnostics status={status || {}} />
              </>
            ),
          },
          {
            key: 'config',
            label: '采集配置',
            children: (
              <Card size="small">
                <Space orientation="vertical" style={{ width: '100%' }}>
                  <ConcurrencySettings />
                  <ClearQueueButton />
                  <Typography.Text type="secondary">
                    清空等待只跳过本轮到期需求，保留已入库数据和分页断点，不取消正在执行的任务。
                  </Typography.Text>
                </Space>
              </Card>
            ),
          },
        ]}
      />
      <Drawer
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={current ? shopLabel(current) : '店铺详情'}
        size="large"
        styles={{ wrapper: { maxWidth: '100vw' }, body: { padding: 20, minWidth: 0 } }}
      >
        {!current ? (
          <Empty description="店铺已移出当前范围" />
        ) : (
          <>
            <Space wrap style={{ marginBottom: 16 }}>
              <Tag>{current.nodeName}</Tag>
              <Tag color={color[current.runState]}>{runLabels[current.runState]}</Tag>
              <Button
                size="small"
                onClick={() => current.current && onOpen?.(current.id, current.current.kind)}
                disabled={!current.current}
              >
                打开对应后台
              </Button>
            </Space>
            <Tabs
              activeKey={drawerTab}
              onChange={setDrawerTab}
              items={[
                {
                  key: 'steps',
                  label: '子步骤',
                  children: (
                    <Table
                      size="small"
                      rowKey="cache_key"
                      columns={stepColumns}
                      dataSource={steps}
                      pagination={{ pageSize: 15, showSizeChanger: false }}
                      scroll={{ x: 1000 }}
                    />
                  ),
                },
                {
                  key: 'timing',
                  label: '阶段耗时',
                  children: (
                    <Space orientation="vertical" size="middle" style={{ width: '100%' }}>
                      <Alert type="info" title="各步骤独立计时，包含并行，累计值不是墙钟总耗时" />
                      {steps.map((s) => (
                        <Card key={s.cache_key} size="small" title={s.name}>
                          <TaskTiming step={s} />
                        </Card>
                      ))}
                    </Space>
                  ),
                },
                {
                  key: 'history',
                  label: '执行记录',
                  children: (
                    <Space orientation="vertical" style={{ width: '100%' }}>
                      <Upload value={current.upload} detail />
                      <Table
                        size="small"
                        rowKey="cache_key"
                        dataSource={steps.filter((s) => s.last_attempt || s.last_success || s.last_error)}
                        pagination={{ pageSize: 10, showSizeChanger: false }}
                        scroll={{ x: 880 }}
                        columns={[
                          { title: '子步骤', dataIndex: 'name', width: 220 },
                          { title: '最近执行', dataIndex: 'last_attempt', render: stamp, width: 180 },
                          { title: '最近成功 / 恢复', dataIndex: 'last_success', render: stamp, width: 180 },
                          {
                            title: '上次失败 / 当前未恢复异常',
                            dataIndex: 'last_error',
                            width: 300,
                            render: (v, s) => (
                              <Typography.Text type={stepState(s) === 'issue' ? 'danger' : 'secondary'}>
                                {v || '无当前异常'}
                              </Typography.Text>
                            ),
                          },
                        ]}
                      />
                      <ExecutionFileHistory key={current.id} shopId={current.id} kinds={steps.map((s) => s.kind)} />
                    </Space>
                  ),
                },
                {
                  key: 'settings',
                  label: '配置',
                  children: (
                    <>
                      <Descriptions
                        column={1}
                        items={[
                          { key: 'node', label: '所属节点', children: current.nodeName },
                          { key: 'state', label: '当前状态', children: current.state },
                          {
                            key: 'note',
                            label: '计划规则',
                            children: '沿用节点及单店配置。同店同业务模块互斥，未开启的任务不会自动启用。',
                          },
                        ]}
                      />
                      <Button
                        icon={<SettingOutlined />}
                        onClick={() => {
                          setSelected(null);
                          setTab('config');
                        }}
                      >
                        前往采集配置
                      </Button>
                    </>
                  ),
                },
              ]}
            />
          </>
        )}
      </Drawer>
    </section>
  );
}
