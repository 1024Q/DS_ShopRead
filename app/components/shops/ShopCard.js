'use client';
import { ActionButton, Choice } from '../shared/AdminControls';
import { shopLabel } from '../../lib/shop-identity.mjs';
import { useState } from 'react';
import { Dropdown, Drawer, Descriptions, Button } from 'antd';
import { DownOutlined } from '@ant-design/icons';
import { platforms } from '../../lib/platforms.mjs';
import { shopStateLabels as states } from '../../lib/fetch-json';
export default function ShopCard({
  health,
  healthError,
  operatorNames,
  onOperator,
  onAddOperator,
  shop,
  busy,
  saving,
  browsers,
  onOperate,
  onView,
  onEdit,
  onDelete,
  onBrowser,
  onMove,
}) {
  const [detail, setDetail] = useState(false);
  const supported = shop.adapter_available !== false,
    stopped = shop.state === 'stopped';
  const viewReady = !stopped && shop.state !== 'sleeping' && shop.state !== 'login_failed';
  const name = shopLabel(shop);
  const items = [
    { key: 'details', label: '店铺详情 / 浏览器', onClick: () => setDetail(true) },
    { key: 'edit', label: '编辑店铺', disabled: busy || saving, onClick: onEdit },
    {
      key: 'qr',
      label: '二维码登录',
      disabled: busy || !supported || shop.state === 'logged_in',
      onClick: () => onOperate('qr-login'),
    },
    {
      key: 'retry',
      label: '重试填入',
      disabled: busy || stopped || !supported || shop.login_method === 'qr',
      onClick: () => onOperate('retry'),
    },
    { key: 'stop', label: '停止浏览器', disabled: busy || stopped || !supported, onClick: () => onOperate('stop') },
    {
      key: 'debug',
      label: shop.debug_mode ? '关闭调试日志' : '开启调试日志（窗口保持隐藏）',
      disabled: busy || !supported,
      onClick: () => onOperate(shop.debug_mode ? 'debug-off' : 'debug-on'),
    },
    ...(shop.node_id && onMove ? [{ key: 'move', label: '切换节点', disabled: busy || saving, onClick: onMove }] : []),
    { type: 'divider' },
    { key: 'delete', label: '删除店铺', danger: true, disabled: busy || saving, onClick: onDelete },
  ];
  return (
    <article className="card shop-card">
      <div className="shop-card-heading">
        <span className="shop-icon">{(shop.name || '店')[0]}</span>
        <div className="shop-card-identity">
          <h3 title={name}>{name}</h3>
          <p className="account" title={shop.username}>
            {shop.username?.startsWith('qr-') ? '扫码登录账号' : shop.username}
          </p>
        </div>
        <span className={`status ${shop.state}`}>{supported ? states[shop.state] || '状态未知' : '待接入'}</span>
      </div>
      <div className="shop-card-meta">
        {shop.machine_name && (
          <span className="shop-node-badge" title={`所属采集节点：${shop.machine_name}`}>
            节点 · {shop.machine_name}
          </span>
        )}
        <span>{platforms[shop.platform || 'pdd']}</span>
        <span>{shop.browser === 'system' ? '系统 Chrome' : '项目 Chrome'}</span>
        <ActionButton
          className="shop-owner-button"
          aria-label={`${name}分配运营负责人`}
          title="支持分配多位运营"
          disabled={busy}
          onClick={onAddOperator}
        >
          运营 · {shop.operator || '未分配'} <span>编辑</span>
        </ActionButton>
        <span>{shop.visible ? '窗口显示中' : '窗口已隐藏'}</span>
        {shop.debug_mode && (
          <span className="shop-debug-badge" title="调试日志已开启；采集窗口隐藏，可手动显示">
            调试模式
          </span>
        )}
      </div>
      {shop.error && (
        <p className="error shop-card-error" title={shop.error}>
          {shop.error}
        </p>
      )}
      {!shop.error && shop.recovery_status && (
        <p className="shop-card-error" title={shop.recovery_status}>
          {shop.recovery_status}
        </p>
      )}
      <div
        className="shop-health-metrics"
        aria-label="近24小时采集健康"
        title={healthError ? healthError + '，保留上次数据' : '近24小时采集结果，每15秒更新'}
      >
        {[
          ['成功率', health?.success_rate == null ? '—' : health.success_rate + '%'],
          ['平均耗时', health?.average_seconds == null ? '—' : health.average_seconds + '秒'],
          ['异常', health?.failed_modules ?? '—'],
          ['最大连续失败', health?.consecutive ?? '—'],
        ].map(([label, value]) => (
          <div
            key={label}
            className={(label === '异常' || label === '最大连续失败') && Number(value) > 0 ? 'health-warning' : ''}
          >
            <span>{label}</span>
            <strong>{value}</strong>
          </div>
        ))}
      </div>
      <div className="shop-card-actions">
        <ActionButton
          className="primary"
          disabled={busy || !supported}
          onClick={() => (viewReady ? onView() : onOperate('start'))}
        >
          {busy
            ? '处理中…'
            : viewReady
              ? shop.state === 'needs_verification'
                ? '完成验证'
                : '登录画面'
              : shop.state === 'sleeping'
                ? '唤醒店铺'
                : '启动 / 登录'}
        </ActionButton>
        <ActionButton
          disabled={busy || stopped || shop.state === 'sleeping' || !supported}
          onClick={() => onOperate(shop.visible ? 'hide' : 'show')}
        >
          {shop.visible ? '隐藏窗口' : '显示窗口'}
        </ActionButton>
        <Dropdown trigger={['click']} menu={{ items }}>
          <Button aria-label={`${name}更多操作`}>
            更多 <DownOutlined />
          </Button>
        </Dropdown>
        <Drawer title={name} open={detail} onClose={() => setDetail(false)} size={460}>
          <Descriptions
            column={1}
            items={[
              { key: 'name', label: '实际店铺', children: shop.detected_name || '登录后自动识别' },
              { key: 'node', label: '采集节点', children: shop.machine_name || '本机' },
              { key: 'port', label: '浏览器端口', children: shop.port ?? '—' },
              { key: 'state', label: '状态', children: supported ? states[shop.state] || '状态未知' : '待接入' },
              {
                key: 'debug',
                label: '调试模式',
                children: shop.debug_mode ? '已开启（窗口保持隐藏）' : '关闭（后台静默采集）',
              },
            ]}
          />
          {shop.error && <p className="error">{shop.error}</p>}
          {supported && stopped && (
            <label className="browser-choice">
              浏览器
              <Choice
                aria-label={`${name}浏览器`}
                value={shop.browser}
                disabled={busy}
                onChange={(e) => onBrowser(e.target.value)}
              >
                {['system', 'bundled'].map((kind) => (
                  <option key={kind} value={kind} disabled={browsers.find((b) => b.id === kind)?.available === false}>
                    {kind === 'system' ? '系统 Google Chrome' : '项目 Google Chrome'}
                  </option>
                ))}
              </Choice>
            </label>
          )}
        </Drawer>
      </div>
    </article>
  );
}
