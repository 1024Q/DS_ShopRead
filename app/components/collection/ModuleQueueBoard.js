'use client';
import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Dropdown, Empty, Input, Modal, Select, Space, Table, Tabs, Tag, Tooltip } from 'antd';
import { HolderOutlined, MoreOutlined, PlusOutlined } from '@ant-design/icons';
import { platforms } from '../../lib/platforms.mjs';
import { commonValue, scopeLabel } from '../../lib/node-policy.mjs';
import { settingsRequest, confirmedAssignments } from '../../lib/module-save.mjs';
import {
  effectiveStepMode,
  queryMembers,
  editorGroups,
  editorSteps,
  stepParent,
  draftStepParent,
  newTaskGroupId,
  confirmedLayoutPatch,
  formatDuration,
  formatCadence,
} from '../../lib/task-layout.mjs';
import NodeConfigDifferences from './NodeConfigDifferences';
import { PipelineScripts, StepScripts, TaskScripts } from './TaskScripts';

const emptyLayoutPatch = () => ({ group_updates: {}, step_assignments: [], group_moves: {} });
const patchSize = (patch) => Object.keys(patch.group_updates).length + patch.step_assignments.length;

export default function ModuleQueueBoard({ config: initialConfig }) {
  const [scope, setScope] = useState(null),
    [loadError, setLoadError] = useState('');
  const [platform, setPlatform] = useState('pdd'),
    [opened, setOpened] = useState(false);
  const [tab, setTab] = useState('tasks'),
    [search, setSearch] = useState('');
  const [layoutPatch, setLayoutPatch] = useState(emptyLayoutPatch),
    [frequencyDraft, setFrequencyDraft] = useState({});
  const [picked, setPicked] = useState(''),
    [over, setOver] = useState('');
  const [busy, setBusy] = useState(false),
    [message, setMessage] = useState(''),
    [results, setResults] = useState([]);
  const [dialog, setDialog] = useState(null),
    [dialogName, setDialogName] = useState(''),
    [dialogTarget, setDialogTarget] = useState('');
  const [dialogError, setDialogError] = useState('');
  const [metrics, setMetrics] = useState({}),
    [metricsStatus, setMetricsStatus] = useState(null),
    [metricsLoading, setMetricsLoading] = useState(false);
  const metricRequest = useRef(0),
    reloadRequest = useRef(0);
  const config = scope?.collection || initialConfig || {},
    nodes = scope?.node_configs || [];
  const configs = nodes.length ? nodes.map((n) => n.collection) : [config];
  const baseGroups = editorGroups(config, nodes);
  const groups = [
    ...baseGroups
      .filter((g) => layoutPatch.group_updates[g.id] !== null)
      .map((g) => ({ ...g, ...layoutPatch.group_updates[g.id] })),
    ...Object.entries(layoutPatch.group_updates)
      .filter(([id, value]) => value && !baseGroups.some((g) => g.id === id))
      .map(([id, value]) => ({ id, ...value })),
  ];
  const catalog = editorSteps(config, nodes, platform);
  const dirty = patchSize(layoutPatch) + Object.keys(frequencyDraft).length;
  const hasLayout = configs.length > 0 && configs.every((c) => c?.task_layout?.version === 1);
  const readOnly = scope?.configuration_read_only || nodes.some((node) => node.online === false || node.error);
  const canEdit = !!scope && hasLayout && !readOnly;
  const baseline = (read) => (nodes.length ? commonValue(nodes, read) : read(config));
  const originalParent = (id) => baseline((c) => stepParent(c, platform, id)) ?? 'mixed';
  const parent = (id) => baseline((c) => draftStepParent(c, platform, id, layoutPatch)) ?? 'mixed';
  const mode = (step) => {
    const values = queryMembers(config, step.id).map(
      (id) =>
        frequencyDraft[`${platform}:${id}`]?.target ?? baseline((c) => effectiveStepMode(c, platform, id)) ?? 'mixed',
    );
    return values.every((v) => v === values[0]) ? values[0] : 'mixed';
  };
  const cadenceGroups = [...new Map(configs.flatMap((c) => c?.frequency_groups || []).map((g) => [g.id, g])).values()];
  const cadenceText = (step) => {
    const id = mode(step);
    if (id === 'off') return '已暂停';
    if (id === 'mixed') return '范围或机器频率不同';
    return (
      baseline((c) => {
        const g = c.frequency_groups?.find((item) => item.id === id);
        return g ? `${g.name} · ${formatCadence(g)}` : '无此频率组';
      }) ?? '各机器间隔不同'
    );
  };
  const timeoutText = (step) => {
    if (mode(step) === 'off') return '—';
    const value = baseline((c) => c.frequency_groups?.find((g) => g.id === mode(step))?.timeout);
    return value == null ? '各机器不同' : formatDuration(value);
  };
  const availableGroup = (g) => !busy && canEdit && (!g.missing || !!layoutPatch.group_updates[g.id]);
  const groupOptions = groups.map((g) => ({
    value: g.id,
    label: g.name + (g.mixed && !layoutPatch.group_updates[g.id] ? '（名称有差异）' : ''),
    disabled: !availableGroup(g),
  }));
  const visibleSteps = catalog.filter(
    (s) => !search || `${s.label} ${s.id}`.toLowerCase().includes(search.toLowerCase()),
  );
  const estimate = (step) => {
    const parts = queryMembers(config, step.id).map((id) => metrics[platform]?.[id]);
    if (parts.some((v) => !v?.sample_count)) return null;
    return {
      sample_count: Math.min(...parts.map((v) => v.sample_count)),
      estimated_read_seconds: parts.reduce((sum, v) => sum + v.estimated_read_seconds, 0),
    };
  };

  async function reload() {
    const request = ++reloadRequest.current;
    try {
      const data = await settingsRequest('GET', undefined, 20000, scope?.scope_node_id ?? null);
      if (reloadRequest.current !== request) return null;
      setScope(data);
      setLoadError('');
      return data;
    } catch (e) {
      if (reloadRequest.current === request) setLoadError(e.message);
      return null;
    }
  }
  useEffect(() => {
    reload();
  }, []);
  useEffect(() => {
    if (!opened || !scope) return;
    const request = ++metricRequest.current;
    setMetricsLoading(true);
    setMetricsStatus(null);
    settingsRequest(
      'GET',
      undefined,
      20000,
      scope.scope_node_id,
      `system/settings?include_step_metrics=true&metrics_platform=${encodeURIComponent(platform)}`,
    )
      .then((data) => {
        if (metricRequest.current !== request) return;
        setMetrics(data.step_metrics || {});
        setMetricsStatus(data.step_metrics_status || { complete: true });
      })
      .catch((e) => {
        if (metricRequest.current === request) {
          setMetrics({});
          setMetricsStatus({ complete: false, error: e.message });
        }
      })
      .finally(() => {
        if (metricRequest.current === request) setMetricsLoading(false);
      });
    return () => {
      metricRequest.current++;
    };
  }, [opened, platform, scope?.scope_node_id]);
  useEffect(() => {
    if (!dirty && !busy) return;
    const unload = (event) => {
      event.preventDefault();
      event.returnValue = '';
    };
    const navigate = (event) => {
      if (busy || !window.confirm('任务设置尚未保存，确定放弃修改并离开？')) event.preventDefault();
    };
    window.addEventListener('beforeunload', unload);
    window.addEventListener('workspace-before-navigate', navigate);
    return () => {
      window.removeEventListener('beforeunload', unload);
      window.removeEventListener('workspace-before-navigate', navigate);
    };
  }, [dirty, busy]);

  function move(stepId, target) {
    const group = groups.find((g) => g.id === target);
    if (!scope || !group || !availableGroup(group) || !catalog.some((s) => s.id === stepId)) return;
    const members = queryMembers(config, stepId);
    setLayoutPatch((old) => ({
      ...old,
      step_assignments: [
        ...old.step_assignments.filter((a) => !(a.platform === platform && members.includes(a.step_id))),
        ...(target === originalParent(stepId) && !old.group_moves[target]
          ? []
          : members.map((id) => ({ platform, step_id: id, group_id: target }))),
      ],
    }));
    setPicked('');
    setOver('');
    setMessage('');
  }
  function changeFrequency(step, target) {
    if (busy || !canEdit || target === 'mixed') return;
    setFrequencyDraft((old) => {
      const next = { ...old };
      for (const id of queryMembers(config, step.id)) {
        const key = `${platform}:${id}`;
        if (target === baseline((c) => effectiveStepMode(c, platform, id))) delete next[key];
        else next[key] = { platform, kind: id, tab: 'step', target };
      }
      return next;
    });
    setMessage('');
  }
  function openDialog(action, group) {
    setDialog({ action, group });
    setDialogName(group?.name || '');
    setDialogTarget('');
    setDialogError('');
  }
  function applyDialog() {
    if (!dialog || busy || !canEdit) return;
    if (dialog.action === 'delete') {
      if (!dialogTarget) {
        setDialogError('请选择剩余子步骤迁入的任务（包含其他平台）。');
        return;
      }
      const source = dialog.group.id;
      setLayoutPatch((old) => {
        const updates = { ...old.group_updates },
          moves = { ...old.group_moves };
        if (baseGroups.some((g) => g.id === source)) {
          updates[source] = null;
          moves[source] = dialogTarget;
        } else delete updates[source];
        for (const [id, target] of Object.entries(moves)) if (target === source) moves[id] = dialogTarget;
        return {
          group_updates: updates,
          group_moves: moves,
          step_assignments: old.step_assignments
            .map((a) => (a.group_id === source ? { ...a, group_id: dialogTarget } : a))
            .filter((a) => baseline((c) => stepParent(c, a.platform, a.step_id)) !== a.group_id),
        };
      });
    } else {
      const name = dialogName.trim();
      if (!name) {
        setDialogError('请填写任务名称。');
        return;
      }
      if (groups.some((g) => g.id !== dialog.group?.id && g.name === name)) {
        setDialogError('任务名称不能重复。');
        return;
      }
      const id = dialog.group?.id || newTaskGroupId();
      setLayoutPatch((old) => {
        const updates = { ...old.group_updates };
        const same = baseline((c) => c.task_layout?.groups.find((g) => g.id === id)?.name) === name;
        if (same) delete updates[id];
        else updates[id] = { name };
        return { ...old, group_updates: updates };
      });
    }
    setDialog(null);
    setMessage('');
  }
  async function save() {
    if (!dirty || busy || !canEdit) return;
    const before = scope,
      layout = layoutPatch,
      assignments = Object.values(frequencyDraft);
    const body = {
      ...(patchSize(layout) ? { settings_patch: { task_layout_patch: layout } } : {}),
      ...(assignments.length ? { module_assignments: assignments } : {}),
    };
    setBusy(true);
    setMessage('正在保存修改并等待各节点确认…');
    setResults([]);
    let error;
    try {
      const response = await settingsRequest('PUT', body, 20000, scope.scope_node_id);
      setResults(response.results || []);
    } catch (e) {
      error = e;
      setResults(e.results || []);
    }
    try {
      const latest = await reload();
      const machineRows = latest?.node_configs?.length
        ? latest.node_configs
        : [{ id: latest?.scope_node_id ?? 'local', name: '当前节点', collection: latest?.collection }];
      setResults(
        machineRows.map((node) => {
          const previous = before.node_configs?.find((n) => n.id === node.id)?.collection || before.collection;
          const current = { scope_node_id: node.id, collection: node.collection };
          const original = { scope_node_id: node.id, collection: previous };
          const ok =
            node.online !== false &&
            !node.error &&
            !!node.collection &&
            (!patchSize(layout) || confirmedLayoutPatch(layout, current, original)) &&
            confirmedAssignments(assignments, current).length === assignments.length;
          return {
            node: node.name || String(node.id),
            ok,
            state: ok ? 'effective' : node.online === false || node.error ? 'failed' : 'pending',
            reason: ok
              ? '已生效（已读回确认）'
              : node.error || (node.online === false ? '节点离线，草稿保留' : '等待确认，未确认草稿保留'),
          };
        }),
      );
      const layoutOK = !patchSize(layout) || (!!latest && confirmedLayoutPatch(layout, latest, before));
      const confirmed = latest ? confirmedAssignments(assignments, latest) : [];
      const keys = new Set(confirmed.map((a) => `${a.platform}:${a.kind}`));
      if (layoutOK) setLayoutPatch(emptyLayoutPatch());
      setFrequencyDraft((old) => Object.fromEntries(Object.entries(old).filter(([key]) => !keys.has(key))));
      setMessage(
        layoutOK && confirmed.length === assignments.length
          ? '各节点已确认保存。任务归属已更新，频率修改对后续采集生效。'
          : `部分修改尚未获全部节点确认，未确认草稿已保留。${error?.message || '请查看各节点结果后重试。'}`,
      );
    } catch (e) {
      setMessage(`保存结果未确认，草稿已保留：${e.message}`);
    } finally {
      setBusy(false);
    }
  }
  const intervalOf = (step) => {
    const id = mode(step);
    if (id === 'off' || id === 'mixed') return null;
    return baseline((c) => {
      const g = c.frequency_groups?.find((item) => item.id === id);
      return g && !g.time ? g.seconds : null;
    });
  };
  // A read slower than its own interval can never meet the frequency: it is
  // always overdue no matter how the queue is ordered.
  const tooSlow = (step) => {
    const value = estimate(step),
      interval = intervalOf(step);
    return value?.sample_count > 0 && interval > 0 && value.estimated_read_seconds > interval;
  };
  const metricView = (step) => {
    const value = estimate(step);
    const slow = tooSlow(step);
    return value?.sample_count > 0 ? (
      <Tooltip
        title={
          step.members
            ? `各范围最近每批读取耗时的合计估计，各范围至少 ${value.sample_count} 家店铺有样本；不含排队和入库。`
            : `来自 ${value.sample_count} 家店铺各自最近成功批次的读取时长，不含排队和入库；不是历史全量总耗时，实际耗时随数据量变化。`
        }
      >
        <span>
          约 {formatDuration(value.estimated_read_seconds)} <small>· {value.sample_count} 个样本</small>
          {slow && (
            <Tag color="orange" title="单次读取耗时已超过所选频率间隔，此子步骤必然超频；请改用更长的频率或减少范围">
              读取慢于频率
            </Tag>
          )}
        </span>
      </Tooltip>
    ) : (
      <span className="task-step-muted">{metricsLoading ? '读取耗时样本…' : '暂无实测'}</span>
    );
  };
  const columns = [
    {
      title: '子步骤',
      key: 'step',
      width: 230,
      sorter: (a, b) => a.label.localeCompare(b.label, 'zh'),
      render: (_, step) => (
        <span>
          {step.label}
          {step.ranges && <small>（{step.ranges.join('、')}）</small>}
          {step.home && <Tag color="blue">首页</Tag>}
        </span>
      ),
    },
    {
      title: '所属后台任务',
      key: 'parent',
      width: 245,
      render: (_, step) => (
        <Select
          aria-label={`${step.label}所属任务`}
          style={{ width: '100%' }}
          disabled={busy || !canEdit}
          value={parent(step.id)}
          options={[
            ...(parent(step.id) === 'mixed' ? [{ value: 'mixed', label: '范围或机器归属不同', disabled: true }] : []),
            ...groupOptions,
          ]}
          onChange={(value) => move(step.id, value)}
        />
      ),
    },
    {
      title: '所需时间（每批实测）',
      key: 'duration',
      width: 190,
      sorter: (a, b) => (estimate(a)?.estimated_read_seconds ?? -1) - (estimate(b)?.estimated_read_seconds ?? -1),
      render: (_, step) => metricView(step),
    },
    {
      title: '更新频率',
      key: 'frequency',
      width: 255,
      render: (_, step) => (
        <Select
          aria-label={`${step.label}更新频率`}
          style={{ width: '100%' }}
          disabled={busy || !canEdit}
          value={mode(step)}
          options={[
            ...(mode(step) === 'mixed' ? [{ value: 'mixed', label: '范围或机器频率不同', disabled: true }] : []),
            ...cadenceGroups.map((g) => ({
              value: g.id,
              label:
                baseline((c) => {
                  const current = c.frequency_groups?.find((item) => item.id === g.id);
                  return current ? `${current.name} · ${formatCadence(current)}` : undefined;
                }) ?? `${g.name}（各机器名称或间隔不同）`,
              disabled: !configs.every((c) => c?.frequency_groups?.some((item) => item.id === g.id)),
            })),
            { value: 'off', label: '暂停采集' },
          ]}
          onChange={(value) => changeFrequency(step, value)}
        />
      ),
    },
    { title: '读取超时', key: 'timeout', width: 130, render: (_, step) => timeoutText(step) },
  ];
  const cards = [
    ...groups,
    ...(visibleSteps.some((s) => parent(s.id) === 'mixed') ? [{ id: 'mixed', name: '范围或机器归属不同' }] : []),
  ];
  return (
    <details className="module-queue-settings task-layout-settings" onToggle={(e) => setOpened(e.currentTarget.open)}>
      <summary>后台任务与子步骤 · 分组和频率设置</summary>
      <div className="task-editor-toolbar">
        <Space wrap>
          <span>
            应用范围：<b>{scopeLabel(scope)}</b>
          </span>
          <Button disabled={busy} onClick={reload}>
            读取最新配置
          </Button>
        </Space>
        <Space wrap>
          <Select
            aria-label="子步骤平台"
            value={platform}
            disabled={busy}
            options={Object.entries(platforms).map(([value, label]) => ({ value, label }))}
            onChange={(value) => {
              setPlatform(value);
              setPicked('');
            }}
            style={{ width: 140 }}
          />
          <Input.Search
            aria-label="搜索子步骤"
            placeholder="搜索子步骤"
            allowClear
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ width: 230, maxWidth: '100%' }}
          />
        </Space>
      </div>
      <Alert
        type="info"
        showIcon
        title="任务管分组，子步骤管采集频率"
        description="同一查询内的范围分别读取、入库并保留断点，复用工作页面。调整整组频率后点击保存，范围频率一同生效；原有差异和暂停状态在保存前保留。读取超时按每个范围计算。"
      />
      <NodeConfigDifferences nodes={nodes} platform={platform} />
      {loadError && <Alert type="error" title={loadError} showIcon />}
      {scope && !hasLayout && (
        <Alert type="warning" title="部分节点尚未提供任务编排配置，请等待节点更新后读取最新配置。" />
      )}
      {readOnly && <Alert type="warning" title="节点离线，展示上次配置，启动采集服务后可保存。已有草稿保留。" />}
      {metricsStatus?.complete === false && (
        <Alert type="warning" title="部分耗时样本暂不可用，已有样本可供参考。" description={metricsStatus.error} />
      )}
      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          {
            key: 'tasks',
            label: `后台任务（${groups.length}）`,
            children: (
              <>
                <PipelineScripts />
                <div className="task-editor-toolbar">
                  <span>每个子步骤只属于一个后台任务。可拖入任务，也可选中后点击“移入此任务”。</span>
                  <Button icon={<PlusOutlined />} disabled={busy || !canEdit} onClick={() => openDialog('add')}>
                    新增后台任务
                  </Button>
                </div>
                <div className="task-layout-grid">
                  {cards.map((group) => {
                    const steps = visibleSteps.filter((s) => parent(s.id) === group.id),
                      mixed = group.id === 'mixed';
                    return (
                      <section
                        key={group.id}
                        data-task-id={group.id}
                        className={over === group.id ? 'task-layout-drop active' : 'task-layout-drop'}
                        onDragOver={(event) => {
                          if (!mixed && availableGroup(group)) {
                            event.preventDefault();
                            event.dataTransfer.dropEffect = 'move';
                            setOver(group.id);
                          }
                        }}
                        onDragLeave={(event) => {
                          if (!event.currentTarget.contains(event.relatedTarget)) setOver('');
                        }}
                        onDrop={(event) => {
                          event.preventDefault();
                          move(event.dataTransfer.getData('text/plain'), group.id);
                        }}
                      >
                        <Card
                          size="small"
                          title={
                            <span className="task-group-title">
                              {group.name}
                              <Tag>{steps.length}</Tag>
                            </span>
                          }
                          extra={
                            !mixed && (
                              <Dropdown
                                trigger={['click']}
                                menu={{
                                  items: [
                                    { key: 'rename', label: '重命名' },
                                    {
                                      key: 'delete',
                                      label: '删除并转移子步骤',
                                      danger: true,
                                      disabled: groups.length < 2,
                                    },
                                  ],
                                  onClick: ({ key }) => openDialog(key, group),
                                }}
                              >
                                <Button
                                  type="text"
                                  size="small"
                                  aria-label={`${group.name}设置`}
                                  icon={<MoreOutlined />}
                                  disabled={busy || !canEdit}
                                />
                              </Dropdown>
                            )
                          }
                        >
                          {group.mixed && !layoutPatch.group_updates[group.id] && (
                            <Tag color="orange">各机器名称有差异</Tag>
                          )}
                          {group.missing && !layoutPatch.group_updates[group.id] && (
                            <p className="task-step-muted">部分机器无此任务，请选择单台机器设置。</p>
                          )}
                          {picked && !mixed && (
                            <Button
                              size="small"
                              disabled={!availableGroup(group)}
                              onClick={() => move(picked, group.id)}
                            >
                              移入此任务
                            </Button>
                          )}
                          <div className="task-step-list">
                            {steps.length ? (
                              steps.map((step) => (
                                <button
                                  type="button"
                                  className={`task-step-card ${picked === step.id ? 'selected' : ''}`}
                                  key={step.id}
                                  draggable={!busy && canEdit}
                                  disabled={busy || !canEdit}
                                  aria-pressed={picked === step.id}
                                  aria-label={`选择子步骤 ${step.label}`}
                                  onClick={() => setPicked(picked === step.id ? '' : step.id)}
                                  onDragStart={(event) => {
                                    event.dataTransfer.setData('text/plain', step.id);
                                    event.dataTransfer.effectAllowed = 'move';
                                    setPicked(step.id);
                                  }}
                                  onDragEnd={() => setOver('')}
                                >
                                  <span className="task-step-title">
                                    <HolderOutlined />
                                    <span>{step.label}</span>
                                    {step.ranges && <small>{step.ranges.join(' · ')}</small>}
                                    {step.home && <Tag color="blue">首页</Tag>}
                                  </span>
                                  <span className="task-step-meta">
                                    <Tag color={mode(step) === 'off' ? 'default' : 'cyan'}>{cadenceText(step)}</Tag>
                                    <span>{metricView(step)}</span>
                                  </span>
                                </button>
                              ))
                            ) : (
                              <Empty
                                image={Empty.PRESENTED_IMAGE_SIMPLE}
                                description={search ? '无匹配子步骤' : '拖入子步骤'}
                              />
                            )}
                          </div>
                          <TaskScripts steps={steps} platform={platform} />
                        </Card>
                      </section>
                    );
                  })}
                </div>
              </>
            ),
          },
          {
            key: 'steps',
            label: `任务子步骤（${catalog.length}）`,
            children: (
              <>
                <p className="task-step-muted">
                  频率组名称、间隔和超时在“系统设置 → 采集频率与任务超时”中维护。选择“暂停采集”只暂停对应子步骤。
                </p>
                <Table
                  size="small"
                  rowKey="id"
                  columns={columns}
                  dataSource={visibleSteps}
                  expandable={{
                    expandedRowRender: (step) => <StepScripts step={step} platform={platform} />,
                    columnTitle: '脚本',
                  }}
                  pagination={{ pageSize: 20, showSizeChanger: false }}
                  scroll={{ x: 1050 }}
                />
              </>
            ),
          },
        ]}
      />
      <div className="task-editor-save">
        <span>{dirty ? `${dirty} 项未保存修改（包含其他平台）` : '当前设置已保存'}</span>
        <Space>
          <Button
            disabled={busy || !dirty}
            onClick={() => {
              setLayoutPatch(emptyLayoutPatch());
              setFrequencyDraft({});
              setPicked('');
              setMessage('已取消未保存修改。');
            }}
          >
            取消修改
          </Button>
          <Button type="primary" loading={busy} disabled={!dirty || !canEdit} onClick={save}>
            保存任务设置
          </Button>
        </Space>
      </div>
      <p role="status">{message}</p>
      {!!results.length && (
        <Table
          size="small"
          rowKey="node"
          pagination={false}
          aria-label="各节点保存结果"
          dataSource={results}
          columns={[
            { title: '节点', dataIndex: 'node' },
            {
              title: '保存状态',
              render: (_, result) => (
                <Tag color={result.ok ? 'success' : result.state === 'pending' ? 'warning' : 'error'}>
                  {result.ok ? '已生效' : result.state === 'pending' ? '等待确认' : '失败'}
                </Tag>
              ),
            },
            {
              title: '核验结果 / 原因',
              render: (_, result) => result.reason || (result.ok ? '已读回确认' : '未确认，草稿保留'),
            },
          ]}
        />
      )}
      <Modal
        open={!!dialog}
        title={
          dialog?.action === 'delete' ? '删除后台任务' : dialog?.action === 'rename' ? '重命名后台任务' : '新增后台任务'
        }
        onCancel={() => setDialog(null)}
        onOk={applyDialog}
        okText="加入修改草稿"
        okButtonProps={{ disabled: busy || !canEdit }}
        cancelText="取消"
        destroyOnHidden
      >
        {dialog?.action === 'delete' ? (
          <>
            <p>
              删除“{dialog.group.name}”后，将其所有平台的子步骤转入所选任务，采集频率保持不变。点击页面保存按钮后生效。
            </p>
            <Select
              aria-label="删除后迁入任务"
              placeholder="选择迁入任务"
              style={{ width: '100%' }}
              value={dialogTarget || undefined}
              onChange={setDialogTarget}
              options={groupOptions.filter((g) => g.value !== dialog.group.id)}
            />
          </>
        ) : (
          <Input
            aria-label="后台任务名称"
            placeholder="填写任务名称"
            maxLength={80}
            value={dialogName}
            onChange={(e) => setDialogName(e.target.value)}
            onPressEnter={applyDialog}
          />
        )}
        {dialogError && <Alert type="error" title={dialogError} style={{ marginTop: 12 }} />}
      </Modal>
    </details>
  );
}
