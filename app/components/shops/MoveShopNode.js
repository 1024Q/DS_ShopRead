'use client';
import AdminDialog from '../shared/AdminDialog';
import { ActionButton, TextInput, Choice } from '../shared/AdminControls';
import { useEffect, useState } from 'react';
import ShopNodeSelect from './ShopNodeSelect';
import { clearSnapshots } from '../../lib/response-snapshots.mjs';
import { fetchJson } from '../../lib/fetch-json';
import { shopLabel } from '../../lib/shop-identity.mjs';
export default function MoveShopNode({ shop, onClose, onMoved, onOpenLogin, initialNode = '' }) {
  const shops = Array.isArray(shop) ? shop : [shop],
    batch = Array.isArray(shop);
  const [selected, setSelected] = useState(() => shops.map((s) => s.id)),
    [node, setNode] = useState(initialNode),
    [method, setMethod] = useState('password'),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [results, setResults] = useState({});
  const loginMessage = (state) =>
    ({
      logged_in: '已登录 · 按采集计划自动采集',
      needs_verification: '需要验证码或扫码，请打开登录画面完成验证',
      login_failed: '启动或登录失败，请打开店铺重试',
      stopped: '目标店铺未启动，请在店铺卡片重试启动',
    })[state] || '切换成功 · 正在自动登录，成功后恢复采集';
  useEffect(() => {
    if (busy || !Object.values(results).some((r) => r.ok)) return;
    let stopped = false;
    const check = async () => {
      try {
        const current = await fetchJson('/api/shops');
        if (stopped) return;
        setResults((old) => {
          const next = { ...old };
          for (const item of current) {
            if (next[item.id]?.ok && String(item.node_id) === String(next[item.id].target))
              next[item.id] = { ...next[item.id], state: item.state, message: loginMessage(item.state) };
          }
          return next;
        });
      } catch {}
    };
    const timer = setInterval(check, 5000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [busy, node, Object.values(results).some((r) => r.ok)]);
  const pending = shops.filter((s) => selected.includes(s.id) && !results[s.id]?.ok);
  async function submit(e) {
    e.preventDefault();
    if (busy || !node || !pending.length) return;
    setBusy(true);
    setError('');
    let failures = 0;
    for (const s of pending) {
      setResults((old) => ({ ...old, [s.id]: { message: '正在切换…' } }));
      try {
        const result = await fetchJson(`/api/shops/${s.id}/move-node`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ target_node: Number(node), login_method: s.platform === 'wechat' ? 'qr' : method }),
        });
        setResults((old) => ({
          ...old,
          [s.id]: {
            ok: true,
            target: result.node,
            state: result.login_state || 'logging_in',
            message: result.login_error || loginMessage(result.login_state),
          },
        }));
      } catch (e) {
        failures++;
        setResults((old) => ({ ...old, [s.id]: { ok: false, message: e.message } }));
      }
    }
    clearSnapshots();
    setBusy(false);
    await onMoved(node);
    if (failures) setError('部分店铺未完成，可处理原因后重试；成功店铺不会重复切换。');
  }
  return (
    <AdminDialog>
      <form className="admin-dialog-content shop-editor" aria-label="切换采集节点" onSubmit={submit}>
        <div className="dialog-head">
          <h2>{batch ? '批量切换节点' : '切换采集节点'}</h2>
          <ActionButton type="button" disabled={busy} aria-label="关闭" data-dialog-close onClick={onClose}>
            ×
          </ActionButton>
        </div>
        {!batch && (
          <p>
            {shopLabel(shops[0])} · 当前节点：{shops[0].machine_name}
          </p>
        )}
        <ShopNodeSelect
          value={node}
          onChange={setNode}
          exclude={!batch ? shops[0].node_id : undefined}
          disabled={busy}
        />
        <label>
          目标节点登录方式
          <Choice value={method} disabled={busy} onChange={(e) => setMethod(e.target.value)}>
            <option value="password">账号密码登录 · 同步已保存密码</option>
            <option value="qr">重新扫码登录</option>
          </Choice>
        </label>
        <p className="form-hint">
          密码由原节点经中心转发至目标节点，不在页面显示或中心数据库保存。未保存密码的店铺会单独提示；微信小店使用扫码登录。
        </p>
        <div
          style={{
            maxHeight: 280,
            overflowY: 'auto',
            border: '1px solid var(--shell-border)',
            borderRadius: 8,
            padding: 10,
          }}
        >
          {batch && (
            <div className="actions">
              <ActionButton type="button" disabled={busy} onClick={() => setSelected(shops.map((s) => s.id))}>
                全选
              </ActionButton>
              <ActionButton type="button" disabled={busy} onClick={() => setSelected([])}>
                清空
              </ActionButton>
              <span>已选 {selected.length} 家</span>
            </div>
          )}
          {shops.map((s) => (
            <div key={s.id} style={{ padding: '8px 0', borderBottom: '1px solid var(--shell-border)' }}>
              <label style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                {batch && (
                  <TextInput
                    type="checkbox"
                    disabled={busy || results[s.id]?.ok}
                    checked={selected.includes(s.id)}
                    onChange={(e) =>
                      setSelected((old) => (e.target.checked ? [...old, s.id] : old.filter((id) => id !== s.id)))
                    }
                  />
                )}
                <span>
                  {shopLabel(s)} · {s.machine_name}
                </span>
              </label>
              {results[s.id] && (
                <div>
                  <small
                    className={
                      !results[s.id].ok ||
                      ['needs_verification', 'login_failed', 'stopped'].includes(results[s.id].state)
                        ? 'error'
                        : ''
                    }
                    role="status"
                  >
                    {results[s.id].message}
                  </small>
                  {results[s.id].ok && results[s.id].state !== 'logged_in' && (
                    <ActionButton type="button" disabled={busy} onClick={() => onOpenLogin(s.id)}>
                      查看登录 / 验证
                    </ActionButton>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
        <p className="form-hint">
          停止原节点浏览器，保留店铺编号和历史数据。两台节点均需在线；批量逐店执行，自动启动并登录；需验证码的店铺单独处理，不影响其他店铺。
        </p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <ActionButton type="submit" className="primary" disabled={busy || !node || !pending.length}>
          {busy ? '正在逐店切换，请勿关闭…' : `确认切换（${pending.length} 家）`}
        </ActionButton>
      </form>
    </AdminDialog>
  );
}
