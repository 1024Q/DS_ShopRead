'use client';
import { ActionButton, TextInput, Choice } from '../shared/AdminControls';
import { ConfigProvider } from 'antd';
import FrequencyGroups from '../collection/FrequencyGroups';
import PerformanceReport from '../overview/PerformanceReport';
import CollapsibleSection from '../shared/CollapsibleSection';
import DatabaseMaintenance from './DatabaseMaintenance';
import { useEffect, useState } from 'react';
import { useWorkspace } from '../../providers/WorkspaceContext';
import NodeConfigDifferences from '../collection/NodeConfigDifferences';
import { settingsPatch } from '../../lib/settings-patch.mjs';
import { frequencyDefaults } from '../../lib/collection-board.mjs';
import { platforms, hasBusinessAdapter } from '../../lib/platforms.mjs';
import { fetchJson } from '../../lib/fetch-json';
export { platforms };
export default function SystemSettings({ connected, connection }) {
  const [nodeConfigs, setNodeConfigs] = useState([]),
    [savedConfig, setSavedConfig] = useState(null);
  const { theme, setTheme, auto, setAuto } = useWorkspace();
  const [config, setConfig] = useState(null),
    [message, setMessage] = useState(''),
    [saveMessage, setSaveMessage] = useState(null),
    [saving, setSaving] = useState(false);
  useEffect(() => {
    if (!connected) return;
    let active = true;
    setMessage('');
    fetchJson('/api/system/settings')
      .then((d) => {
        if (active) {
          setConfig(d.collection);
          setSavedConfig(d.collection);
          setNodeConfigs(d.node_configs || []);
        }
      })
      .catch((e) => {
        if (active) setMessage(e.message);
      });
    return () => {
      active = false;
    };
  }, [connected]);
  const dirty = !!config && Object.keys(settingsPatch(savedConfig, config)).length > 0;
  useEffect(() => {
    if (!dirty) return;
    const prevent = (e) => {
      e.preventDefault();
      e.returnValue = '';
    };
    const guard = (e) => {
      if (!window.confirm('采集设置尚未保存，确定放弃修改并离开？')) e.preventDefault();
    };
    window.addEventListener('beforeunload', prevent);
    window.addEventListener('workspace-before-navigate', guard);
    return () => {
      window.removeEventListener('beforeunload', prevent);
      window.removeEventListener('workspace-before-navigate', guard);
    };
  }, [dirty]);
  const change = (key, value) => setConfig((c) => ({ ...c, [key]: value }));
  async function save(e) {
    e.preventDefault();
    if (!dirty) return;
    setSaving(true);
    setSaveMessage(null);
    try {
      await fetchJson('/api/system/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings_patch: settingsPatch(savedConfig, config) }),
      });
      const d = await fetchJson('/api/system/settings');
      setConfig(d.collection);
      setSavedConfig(d.collection);
      setNodeConfigs(d.node_configs || []);
      setSaveMessage({ ok: true, text: '修改项已保存；未修改配置保持各节点原值。' });
    } catch (e) {
      setSaveMessage({ ok: false, text: e.message });
    } finally {
      setSaving(false);
    }
  }
  const sections = [
    ['appearance', '外观与连接', true],
    ['schedule', '采集与维护', !!config],
    ['database', '数据库与备份', connected],
  ].filter((s) => s[2]);
  return (
    <section className="business-panel settings-page">
      <div className="business-heading">
        <div>
          <h2>系统设置</h2>
        </div>
      </div>
      <nav className="settings-jump-nav" aria-label="系统设置分组">
        {sections.map(([id, label], index) => (
          <a key={id} href={'#settings-' + id}>
            <small>{String(index + 1).padStart(2, '0')}</small>
            {label}
            <span>↗</span>
          </a>
        ))}
      </nav>
      <div className="settings-basics" id="settings-appearance">
        <section className="settings-card">
          <div className="section-head">
            <div>
              <h3>API 连接</h3>
              <span className={`settings-connection ${connected ? 'connected' : ''}`}>
                {connected ? '服务已连接' : '尚未连接'}
              </span>
            </div>
            <a href="/center">中心与节点连接</a>
          </div>
        </section>
        <CollapsibleSection className="settings-card">
          <h3>外观与显示</h3>
          <div className="theme-options">
            {[
              ['white', '白色', '#ffffff'],
              ['dark', '暗色', '#172338'],
              ['blue', '蓝色', '#2563eb'],
            ].map(([id, label, color]) => (
              <ActionButton
                key={id}
                className={theme === id ? 'selected' : ''}
                aria-pressed={theme === id}
                onClick={() => setTheme(id)}
              >
                <i style={{ background: color }} />
                {label}
                {theme === id ? ' · 当前' : ''}
              </ActionButton>
            ))}
          </div>
          <label className="inline-setting">
            界面数据更新
            <Choice
              aria-label="全局自动刷新"
              value={auto ? '60' : 'off'}
              onChange={(e) => setAuto(e.target.value === '60')}
            >
              <option value="60">数据变化后更新（断线自动兜底）</option>
              <option value="off">暂停界面自动更新</option>
            </Choice>
          </label>
          <p>关闭页面或暂停界面更新，服务端仍按下方计划采集。</p>
        </CollapsibleSection>
      </div>
      {message && (
        <p role="alert" className="notice">
          {message}
        </p>
      )}
      {connected && !config && !message && (
        <p role="status" className="module-loading">
          正在读取采集设置…
        </p>
      )}
      {config && (
        <form onSubmit={save}>
          <ConfigProvider componentDisabled={saving}>
            <fieldset disabled={saving} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
              <CollapsibleSection id="settings-schedule" className="settings-card">
                <h3>后台采集计划</h3>
                <NodeConfigDifferences nodes={nodeConfigs} />
                <p>
                  这里定义可扩展的频率分组、执行间隔与任务超时；模块归属统一在“同步与数据源”中设置。多机器时以下表单以首个可用节点为参考，只保存修改项。
                </p>
                <div className="settings-grid">
                  <h4 className="setting-group-title">采集频率</h4>
                  <label>
                    自动采集
                    <Choice
                      value={config.enabled ? 'on' : 'off'}
                      onChange={(e) => change('enabled', e.target.value === 'on')}
                    >
                      <option value="on">开启</option>
                      <option value="off">暂停</option>
                    </Choice>
                  </label>
                  <FrequencyGroups config={config} onChange={setConfig} />
                  <h4 className="setting-group-title">分批读取</h4>
                  <label>
                    每批最多读取页数
                    <TextInput
                      aria-label="每批最多读取页数"
                      type="number"
                      min="2"
                      max="50"
                      required
                      value={config.batch_pages ?? 12}
                      onChange={(e) => change('batch_pages', Number(e.target.value))}
                    />
                  </label>
                  <label>
                    每批目标时长（秒）
                    <TextInput
                      aria-label="每批目标时长"
                      type="number"
                      min="5"
                      max="60"
                      required
                      value={config.batch_seconds ?? 20}
                      onChange={(e) => change('batch_seconds', Number(e.target.value))}
                    />
                  </label>
                  <h4 className="setting-group-title">登录维护</h4>
                  <label>
                    登录维护
                    <Choice
                      value={config.keepalive_enabled ? 'on' : 'off'}
                      onChange={(e) => change('keepalive_enabled', e.target.value === 'on')}
                    >
                      <option value="on">开启定时检查与刷新</option>
                      <option value="off">关闭</option>
                    </Choice>
                  </label>
                  <label>
                    登录维护间隔
                    <Choice
                      value={config.keepalive_seconds}
                      onChange={(e) => change('keepalive_seconds', Number(e.target.value))}
                    >
                      {[300, 900, 1800, 3600, 7200, 86400].map((s) => (
                        <option key={s} value={s}>
                          每 {s / 60} 分钟
                        </option>
                      ))}
                    </Choice>
                  </label>
                </div>
                <details className="context-note">
                  <summary>采集与登录维护说明</summary>
                  <p>
                    页面读取已存数据；缺失范围自动补采，手动采集进入队列。重点状态与完整列表都按模块所属分组频率采集；同日已完成的每日任务不重复采集。
                  </p>
                  <p>
                    异常任务逐步降低重试频率；权限或适配异常需修复后重试。浏览器忙碌或等待验证码时跳过登录维护，验证码需人工完成。请保持服务运行。
                  </p>
                </details>
              </CollapsibleSection>
              <div className="settings-save sticky-save">
                <span role="status">
                  {saving ? '正在保存…' : dirty ? '有未保存的修改' : saveMessage?.ok ? saveMessage.text : '设置已保存'}
                </span>
                {saveMessage && !saveMessage.ok && !saving && (
                  <span role="alert" className="error">
                    {saveMessage.text}
                  </span>
                )}
                <ActionButton
                  type="button"
                  disabled={saving || JSON.stringify(config) === JSON.stringify(savedConfig)}
                  onClick={() => setConfig(savedConfig)}
                >
                  撤销修改
                </ActionButton>
                <ActionButton type="submit" className="primary" disabled={saving || !dirty}>
                  {saving ? '保存中…' : '保存采集设置'}
                </ActionButton>
              </div>
            </fieldset>
          </ConfigProvider>
        </form>
      )}
      <PerformanceReport />
      {connected && (
        <div id="settings-database">
          <DatabaseMaintenance />
        </div>
      )}
    </section>
  );
}
