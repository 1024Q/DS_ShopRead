'use client';
import AdminDialog from '../shared/AdminDialog';
import { NativeButton, TextInput } from '../shared/AdminControls';
// Shared center node management: one online rule, one add/rename/remove flow and the one-time secret dialog for both /center and the workspace machine page.
import { useState, useEffect } from 'react';
import { fetchJson } from '../../lib/fetch-json';

// Prefer the live tunnel flag (/api/nodes); fall back to the heartbeat age used by the center backend (collector_online: seen within 60s).
export function nodeOnline(n) {
  return typeof n?.online === 'boolean' ? n.online : !!n?.seen && Date.now() / 1000 - n.seen < 60;
}

export async function centerApi(path, body, method = body === undefined ? 'GET' : 'POST') {
  try {
    return await fetchJson('/api/center/' + path, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    if (e.status !== 401 && typeof e.data?.error === 'string') e.message = e.data.error;
    throw e;
  }
}

async function copyText(value) {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {}
  try {
    const area = document.createElement('textarea');
    area.value = value;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  } catch {
    return false;
  }
}

function SecretField({ label, value }) {
  const [state, setState] = useState('');
  return (
    <div className="node-secret-field">
      <label>
        {label}
        <TextInput readOnly value={value} onFocus={(e) => e.target.select()} />
      </label>
      <NativeButton
        type="button"
        onClick={async () => {
          setState((await copyText(value)) ? '已复制' : '复制失败，请手动选择');
          setTimeout(() => setState(''), 2500);
        }}
      >
        {state || '复制'}
      </NativeButton>
    </div>
  );
}

// No backdrop / Esc close: the token is shown only once, so closing requires the explicit confirmation button.
export function NodeSecretDialog({ secret, onClose }) {
  return (
    <AdminDialog label="配置采集节点">
      <section className="admin-dialog-content node-secret-dialog" aria-labelledby="node-secret-title">
        <h3 id="node-secret-title">配置采集节点</h3>
        <p>
          将下方 ID 与密钥填写到该采集机的环境变量。<strong>密钥只显示本次</strong>，关闭后无法再次查看。
        </p>
        <SecretField label="ECMP_NODE_ID" value={secret.id} />
        <SecretField label="ECMP_NODE_TOKEN" value={secret.token} />
        <NativeButton data-dialog-close className="primary" onClick={onClose}>
          已保存，关闭
        </NativeButton>
      </section>
    </AdminDialog>
  );
}

export function NodeCard({ node, busy, onRename, onRemove, children }) {
  const [mode, setMode] = useState(''),
    [name, setName] = useState(node.name),
    online = nodeOnline(node),
    status = node.collector_status || {};
  const working = busy === node.node;
  return (
    <section className="settings-card node-card">
      {mode === 'rename' ? (
        <form
          className="node-rename"
          onSubmit={async (e) => {
            e.preventDefault();
            if (await onRename(node, name.trim())) setMode('');
          }}
        >
          <label>
            机器名称
            <TextInput autoFocus value={name} maxLength={80} required onChange={(e) => setName(e.target.value)} />
          </label>
          <div className="actions">
            <NativeButton className="primary" disabled={!!busy || !name.trim()}>
              {working ? '保存中…' : '保存'}
            </NativeButton>
            <NativeButton type="button" disabled={!!busy} onClick={() => setMode('')}>
              取消
            </NativeButton>
          </div>
        </form>
      ) : (
        <div className="section-head">
          <h3>{node.name}</h3>
          <div className="actions">
            <NativeButton
              disabled={!!busy}
              onClick={() => {
                setName(node.name);
                setMode('rename');
              }}
            >
              改名
            </NativeButton>
            <NativeButton className="danger-text" disabled={!!busy} onClick={() => setMode('remove')}>
              移除
            </NativeButton>
          </div>
        </div>
      )}
      {mode === 'remove' && (
        <div className="node-remove-confirm" role="alert">
          <p>移除「{node.name}」？将撤销其中心访问权限并断开远程连接；中心已保存的数据和采集电脑上的店铺都会保留。</p>
          <div className="actions">
            <NativeButton disabled={!!busy} onClick={() => setMode('')}>
              取消
            </NativeButton>
            <NativeButton
              className="danger"
              disabled={!!busy}
              onClick={async () => {
                if (!(await onRemove(node))) setMode('');
              }}
            >
              {working ? '移除中…' : '确认移除'}
            </NativeButton>
          </div>
        </div>
      )}
      <span className={online ? 'remote-online' : 'node-offline'}>
        {online ? '在线 · 主动连接已建立' : '离线 · 保留已存数据'}
      </span>
      <p>
        采集版本：{status.version || '未接入自动升级'}
        {status.update ? ' · ' + status.update : ''}
      </p>
      {status.clock_warning && (
        <p role="alert" className="error">
          节点时钟与中心相差约 {Math.round(Math.abs(Number(status.clock_offset_seconds) || 0))} 秒，请检查系统时间同步。
        </p>
      )}
      {children}
      <small className="node-id">{node.node}</small>
    </section>
  );
}

// nodes: center registry rows (each has `node` uuid). onChanged reloads the caller's data; onError reports failures.
export default function NodeManager({ nodes, onChanged, onError, renderDetails }) {
  const [name, setName] = useState(''),
    [secret, setSecret] = useState(null),
    [busy, setBusy] = useState('');
  const [pending, setPending] = useState([]),
    [approvalError, setApprovalError] = useState('');
  async function approvals() {
    try {
      setPending(await centerApi('enrollments'));
      setApprovalError('');
    } catch (e) {
      setApprovalError(e.message);
    }
  }
  useEffect(() => {
    let alive = true;
    const read = async () => {
      try {
        const rows = await centerApi('enrollments');
        if (alive) {
          setPending(rows);
          setApprovalError('');
        }
      } catch (e) {
        if (alive) setApprovalError(e.message);
      }
    };
    read();
    const timer = setInterval(() => {
      if (!document.hidden) read();
    }, 10000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);
  async function run(id, callback) {
    if (busy) return false;
    setBusy(id);
    try {
      await callback();
      onError('');
      await onChanged();
      return true;
    } catch (e) {
      onError(e.message);
      return false;
    } finally {
      setBusy('');
    }
  }
  const add = (e) => {
    e.preventDefault();
    run('new', async () => {
      setSecret(await centerApi('nodes', { name: name.trim() }));
      setName('');
    });
  };
  const rename = (node, value) =>
    value ? run(node.node, () => centerApi('nodes/' + node.node, { name: value }, 'PUT')) : false;
  const remove = async (node) => {
    const ok = await run(node.node, () => centerApi('nodes/' + node.node, undefined, 'DELETE'));
    if (ok && secret?.id === node.node) setSecret(null);
    return ok;
  };
  return (
    <>
      <section className="settings-card">
        <div className="section-head">
          <h3>机器接入申请 {pending.length > 0 && `(${pending.length})`}</h3>
          <NativeButton disabled={!!busy} onClick={approvals}>
            刷新申请
          </NativeButton>
        </div>
        <p>新客户端只需填写中心地址。请核对机器名称和客户端核对码，再批准接入。</p>
        {approvalError && (
          <p role="alert" className="error">
            {approvalError}
          </p>
        )}
        {!pending.length && !approvalError && <p>暂无待确认机器</p>}
        <div className="remote-grid">
          {pending.map((item) => (
            <section className="settings-card" key={item.id}>
              <h4>{item.name}</h4>
              <p>
                来源：{item.address} · {new Date(item.created * 1000).toLocaleString('zh-CN')}
              </p>
              <p>
                核对码：<strong>{item.code}</strong>
              </p>
              <small>客户端版本：{item.version || '未提供'}</small>
              <div className="actions">
                <NativeButton
                  className="primary"
                  disabled={!!busy}
                  onClick={() =>
                    run(item.id, async () => {
                      await centerApi('enrollments/' + item.id, { action: 'approve' });
                      await approvals();
                    })
                  }
                >
                  批准接入
                </NativeButton>
                <NativeButton
                  disabled={!!busy}
                  onClick={() =>
                    run(item.id, async () => {
                      await centerApi('enrollments/' + item.id, { action: 'reject' });
                      await approvals();
                    })
                  }
                >
                  拒绝
                </NativeButton>
              </div>
            </section>
          ))}
        </div>
      </section>
      <details>
        <summary>兼容旧客户端：手动添加节点</summary>
        <form className="filter-panel node-add" onSubmit={add}>
          <label>
            机器名称
            <TextInput
              value={name}
              required
              maxLength={80}
              onChange={(e) => setName(e.target.value)}
              placeholder="例如：仓库采集机"
            />
          </label>
          <NativeButton className="primary" disabled={!!busy || !name.trim()}>
            {busy === 'new' ? '添加中…' : '添加采集机器'}
          </NativeButton>
        </form>
      </details>
      <div className="remote-grid">
        {nodes.map((n) => (
          <NodeCard key={n.node} node={n} busy={busy} onRename={rename} onRemove={remove}>
            {renderDetails?.(n)}
          </NodeCard>
        ))}
      </div>
      {!nodes.length && <div className="empty">尚未接入采集机器。请在客户端填写中心地址，再在此处批准申请。</div>}
      {secret && <NodeSecretDialog secret={secret} onClose={() => setSecret(null)} />}
    </>
  );
}
