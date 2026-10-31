'use client';
import { useState } from 'react';
import { Alert, Button, Card, Empty, Input, Select, Space, Tag, Typography, Popconfirm } from 'antd';
import { platforms } from '../../lib/platforms.mjs';
import { shopLabel } from '../../lib/shop-identity.mjs';
import { shopStateLabels } from '../../lib/fetch-json';
export default function BrowserWorkspace({ shops, connected, busy, onOperate, onView, onRefresh, onAll }) {
  const [search, setSearch] = useState(''),
    [node, setNode] = useState(),
    [platform, setPlatform] = useState(),
    [state, setState] = useState();
  const nodes = [
    ...new Map(
      shops.map((s) => [
        String(s.node_id || 'local'),
        { value: String(s.node_id || 'local'), label: s.machine_name || '本机' },
      ]),
    ).values(),
  ];
  const visible = shops.filter(
    (s) =>
      (!node || String(s.node_id || 'local') === node) &&
      (!platform || (s.platform || 'pdd') === platform) &&
      (!state || s.state === state) &&
      (!search || `${shopLabel(s)} ${s.username || ''}`.toLowerCase().includes(search.toLowerCase())),
  );
  return (
    <section aria-label="店铺浏览器工作台">
      <Space wrap style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
        <div>
          <Typography.Title level={2} style={{ margin: 0 }}>
            店铺浏览器工作台
          </Typography.Title>
          <Typography.Text type="secondary">集中管理 · 每店独立 Google Chrome 登录目录</Typography.Text>
        </div>
        <Space wrap>
          <Button disabled={!connected} onClick={onAll}>
            查看全部节点
          </Button>
          <Button disabled={!connected} onClick={onRefresh}>
            刷新状态
          </Button>
        </Space>
      </Space>
      <Alert
        type="info"
        showIcon
        title="只打开需要操作的店铺，不会因进入此页面启动所有浏览器。"
        description="登录画面可在当前后台查看；显示窗口在店铺所属采集机器上打开。各店铺保持独立登录，继续使用现有采集频率、并发和休眠设置。"
        style={{ marginBottom: 16 }}
      />
      <Card size="small" style={{ marginBottom: 16 }}>
        <Space wrap>
          <Input.Search
            aria-label="搜索店铺浏览器"
            placeholder="店铺名称 / 账号"
            allowClear
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ width: 240, maxWidth: '100%' }}
          />
          <Select
            aria-label="筛选采集节点"
            placeholder="全部节点"
            allowClear
            value={node}
            onChange={setNode}
            options={nodes}
            style={{ width: 180 }}
          />
          <Select
            aria-label="筛选平台"
            placeholder="全部平台"
            allowClear
            value={platform}
            onChange={setPlatform}
            options={Object.entries(platforms).map(([value, label]) => ({ value, label }))}
            style={{ width: 150 }}
          />
          <Select
            aria-label="筛选登录状态"
            placeholder="全部状态"
            allowClear
            value={state}
            onChange={setState}
            options={Object.entries(shopStateLabels).map(([value, label]) => ({ value, label }))}
            style={{ width: 160 }}
          />
          <Tag>
            {visible.length} / {shops.length} 家
          </Tag>
          <Tag color="green">已登录 {shops.filter((s) => s.state === 'logged_in').length} 家</Tag>
        </Space>
      </Card>
      {!connected ? (
        <Empty description="请先连接中心服务" />
      ) : !visible.length ? (
        <Empty description="当前筛选下没有店铺" />
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(min(100%,320px),1fr))', gap: 16 }}>
          {visible.map((shop) => {
            const inactive = ['stopped', 'sleeping', 'login_failed'].includes(shop.state),
              disabled = !!busy[shop.id] || shop.adapter_available === false;
            return (
              <Card
                key={shop.id}
                size="small"
                title={
                  <span title={shopLabel(shop)} style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>
                    {shopLabel(shop)}
                  </span>
                }
                extra={
                  <Tag
                    color={
                      shop.state === 'logged_in' ? 'green' : shop.state === 'needs_verification' ? 'orange' : 'default'
                    }
                  >
                    {shopStateLabels[shop.state] || '状态未知'}
                  </Tag>
                }
              >
                <Space orientation="vertical" style={{ width: '100%' }}>
                  <Typography.Text type="secondary">
                    {shop.machine_name || '本机'} · {platforms[shop.platform || 'pdd']} ·{' '}
                    {shop.browser === 'bundled' ? '项目 Chrome' : '系统 Chrome'}
                  </Typography.Text>
                  <Typography.Text type="secondary">
                    {shop.visible ? '窗口显示中' : '窗口未显示'} · 独立登录
                  </Typography.Text>
                  {(shop.error || shop.recovery_status) && (
                    <Alert type={shop.error ? 'warning' : 'info'} title={shop.error || shop.recovery_status} />
                  )}
                  <Space wrap>
                    <Button
                      type="primary"
                      loading={!!busy[shop.id]}
                      disabled={disabled}
                      onClick={() => (inactive ? onOperate(shop, 'start') : onView(shop))}
                    >
                      {inactive ? (shop.state === 'sleeping' ? '唤醒 / 登录' : '启动 / 登录') : '登录画面'}
                    </Button>
                    <Button
                      disabled={disabled || inactive}
                      onClick={() => onOperate(shop, shop.visible ? 'hide' : 'show')}
                    >
                      {shop.visible ? '隐藏窗口' : '显示窗口'}
                    </Button>
                    <Popconfirm
                      title="停止这家店铺的浏览器？"
                      description="正在读取的任务可能中断，已保存数据保留。"
                      onConfirm={() => onOperate(shop, 'stop')}
                    >
                      <Button disabled={disabled || shop.state === 'stopped'}>停止</Button>
                    </Popconfirm>
                  </Space>
                </Space>
              </Card>
            );
          })}
        </div>
      )}
    </section>
  );
}
