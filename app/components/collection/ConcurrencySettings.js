'use client';
import { Alert, ConfigProvider } from 'antd';
import { ActionButton, TextInput } from '../shared/AdminControls';
import { useEffect, useState } from 'react';
import { settingsRequest } from '../../lib/module-save.mjs';
import { changedValue } from '../../lib/node-policy.mjs';
const fields = ['max_concurrent', 'shop_concurrent', 'max_active_shops', 'browser_idle_seconds'];
const labels = ['节点总并发', '默认单店并发', '同时采集店铺数', '浏览器空闲关闭（秒，0 不关闭）'];
export default function ConcurrencySettings() {
  const [data, setData] = useState(null),
    [drafts, setDrafts] = useState({}),
    [saving, setSaving] = useState({}),
    [messages, setMessages] = useState({}),
    [loading, setLoading] = useState(false),
    [error, setError] = useState(''),
    [overrideText, setOverrideText] = useState({});
  const nodes = data?.node_configs || [],
    dirty = Object.values(drafts).some((p) => Object.keys(p).length),
    busy = Object.values(saving).some(Boolean);
  useEffect(() => {
    if (!dirty && !busy) return;
    const guard = (e) => {
      if (busy || !window.confirm('机器采集参数尚未保存，确定离开？')) e.preventDefault();
    };
    const unload = (e) => {
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('workspace-before-navigate', guard);
    window.addEventListener('beforeunload', unload);
    return () => {
      window.removeEventListener('workspace-before-navigate', guard);
      window.removeEventListener('beforeunload', unload);
    };
  }, [dirty, busy]);
  async function load() {
    if (dirty && !window.confirm('放弃未保存修改并读取最新配置？')) return;
    setLoading(true);
    setError('');
    try {
      setData(await settingsRequest('GET', undefined, 20000, data?.scope_node_id ?? null));
      setDrafts({});
      setMessages({});
      setOverrideText({});
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }
  const change = (node, key, value) => {
    setDrafts((d) => ({ ...d, [node.id]: changedValue([node], key, value, d[node.id] || {}) }));
    setMessages((m) => ({ ...m, [node.id]: null }));
  };
  async function save(e, node) {
    e.preventDefault();
    const patch = drafts[node.id] || {};
    if (saving[node.id] || !Object.keys(patch).length) return;
    setSaving((s) => ({ ...s, [node.id]: true }));
    setMessages((m) => ({ ...m, [node.id]: { text: '等待确认 · 正在保存并读回实际配置…' } }));
    try {
      await settingsRequest('PUT', patch, 25000, node.id, 'system/concurrency');
      const fresh = await settingsRequest('GET', undefined, 20000, node.id),
        actual = fresh.node_configs?.find((n) => n.id === node.id);
      if (!actual?.collection) throw Error('无法读取机器最新配置，修改项已保留');
      setData((d) => ({ ...d, node_configs: d.node_configs.map((n) => (n.id === node.id ? actual : n)) }));
      const remaining = Object.fromEntries(
        Object.entries(patch).filter(([k, v]) => JSON.stringify(actual.collection[k]) !== JSON.stringify(v)),
      );
      setDrafts((d) => ({ ...d, [node.id]: remaining }));
      setMessages((m) => ({
        ...m,
        [node.id]: {
          ok: !Object.keys(remaining).length,
          text: Object.keys(remaining).length ? '部分修改未确认生效，已保留修改项' : '已生效（已读回确认）',
        },
      }));
    } catch (e) {
      setMessages((m) => ({
        ...m,
        [node.id]: { ok: false, text: (/超时|未确认|暂不可/.test(e.message) ? '等待确认：' : '失败：') + e.message },
      }));
    } finally {
      setSaving((s) => ({ ...s, [node.id]: false }));
    }
  }
  return (
    <div className="concurrency-settings">
      <ActionButton disabled={loading || busy} onClick={load}>
        {loading ? '读取机器配置…' : data ? '刷新机器配置' : '采集机器参数'}
      </ActionButton>
      {dirty && (
        <Alert
          type="warning"
          showIcon
          title={`有 ${Object.values(drafts).reduce((n, p) => n + Object.keys(p).length, 0)} 项采集参数未保存`}
        />
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {data && (
        <>
          <p className="node-parameter-note">
            每台机器独立保存，仅提交修改项。总并发无固定数值上限，默认节点任务并发 20、单店 3、同时 2 家店铺，空闲 30
            秒关闭浏览器，可自行调整。
          </p>
          <div className="node-parameter-grid">
            {nodes.map((node) => {
              const patch = drafts[node.id] || {},
                value = { ...node.collection, ...patch },
                changed = Object.keys(patch).length > 0;
              const override =
                overrideText[node.id] ??
                Object.entries(value.shop_limits || {})
                  .map(([id, n]) => id + ':' + n)
                  .join('\n');
              const pairs = override.trim()
                ? override
                    .trim()
                    .split(/\n/)
                    .map((v) => v.split(':').map((s) => s.trim()))
                : [];
              const overridesValid =
                pairs.every(
                  (p) => p.length === 2 && /^\d+$/.test(p[0]) && Number.isSafeInteger(Number(p[1])) && Number(p[1]) > 0,
                ) && new Set(pairs.map((p) => p[0])).size === pairs.length;
              const valid =
                overridesValid &&
                fields.every(
                  (k) => Number.isSafeInteger(value[k]) && value[k] >= (k === 'browser_idle_seconds' ? 0 : 1),
                );
              const message = messages[node.id];
              return (
                <form
                  key={node.id}
                  className="node-parameter-card"
                  aria-label={`${node.name}采集参数`}
                  onSubmit={(e) => save(e, node)}
                >
                  <header>
                    <strong>{node.name}</strong>
                    <span className={node.online ? 'node-online' : 'node-offline'}>
                      {node.online ? '在线' : '离线 / 未连接'}
                    </span>
                  </header>
                  {!node.collection ? (
                    <p className="error">{node.error || '暂时无法读取配置，请连接节点后刷新。'}</p>
                  ) : (
                    <ConfigProvider componentDisabled={loading || saving[node.id]}>
                      <fieldset disabled={loading || saving[node.id]}>
                        <div className="node-parameter-fields">
                          {fields.map((key, i) => (
                            <label key={key}>
                              {labels[i]}
                              <TextInput
                                aria-label={labels[i]}
                                type="number"
                                min={key === 'browser_idle_seconds' ? 0 : 1}
                                step="1"
                                value={value[key] ?? ''}
                                onChange={(e) => change(node, key, e.target.value === '' ? '' : Number(e.target.value))}
                              />
                            </label>
                          ))}
                          <label>
                            单店并发覆盖（节点内店铺编号:并发，每行一项）
                            <textarea
                              value={override}
                              onChange={(e) => {
                                setOverrideText((v) => ({ ...v, [node.id]: e.target.value }));
                                const pairs = e.target.value.trim()
                                  ? e.target.value
                                      .trim()
                                      .split(/\n/)
                                      .map((v) => v.split(':').map((s) => s.trim()))
                                  : [];
                                if (
                                  pairs.every(
                                    (p) =>
                                      p.length === 2 &&
                                      /^\d+$/.test(p[0]) &&
                                      Number.isSafeInteger(Number(p[1])) &&
                                      Number(p[1]) > 0,
                                  )
                                )
                                  change(
                                    node,
                                    'shop_limits',
                                    Object.fromEntries(pairs.map(([id, n]) => [id, Number(n)])),
                                  );
                              }}
                            />
                          </label>
                        </div>
                        <p className={valid ? 'node-parameter-help' : 'error'}>
                          {valid
                            ? '按店铺轮转分配名额，保留节点与单店任务并发；同店同业务模块串行。'
                            : '并发必须为正整数；覆盖设置需按“店铺编号:并发”填写且编号不能重复。'}
                        </p>
                        <footer>
                          <span>{changed ? `${Object.keys(patch).length} 项未保存` : '配置已同步'}</span>
                          <ActionButton
                            type="button"
                            disabled={!changed}
                            onClick={() => {
                              setDrafts((d) => ({ ...d, [node.id]: {} }));
                              setOverrideText((v) => {
                                const n = { ...v };
                                delete n[node.id];
                                return n;
                              });
                              setMessages((m) => ({ ...m, [node.id]: null }));
                            }}
                          >
                            取消修改
                          </ActionButton>
                          <ActionButton className="primary" type="submit" disabled={!changed || !valid}>
                            {saving[node.id] ? '确认中…' : '保存本机配置'}
                          </ActionButton>
                        </footer>
                      </fieldset>
                    </ConfigProvider>
                  )}
                  {message && (
                    <p
                      role="status"
                      className={message.ok === false ? 'error' : message.ok ? 'node-online' : 'node-parameter-help'}
                    >
                      {message.text}
                    </p>
                  )}
                </form>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
