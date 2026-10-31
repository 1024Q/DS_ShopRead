'use client';
import { uploadBody } from '../../lib/upload-body.mjs';
import SortableTable from '../shared/SortableTable';
import AdminDialog from '../shared/AdminDialog';
import { NativeButton, TextInput, Choice } from '../shared/AdminControls';
import { useEffect, useRef, useState } from 'react';
import { fetchJson } from '../../lib/fetch-json';
const names = {
  ready: '待确认',
  exception: '需处理',
  queued: '排队上传',
  uploading: '正在上传',
  submitting: '正在提交',
  verify_pending: '待平台核验',
  success: '上传成功',
  cancelled: '已移除',
};
const money = (n) => (n == null ? '—' : `¥${(n / 100).toFixed(2)}`);
async function api(path, body) {
  return fetchJson('/api/invoice-imports' + path, {
    method: body === undefined ? 'GET' : 'POST',
    ...(body === undefined ? {} : uploadBody(body)),
  });
}
export default function InvoiceImport({ onUpdated }) {
  const [open, setOpen] = useState(false),
    [batch, setBatch] = useState(null),
    [history, setHistory] = useState([]),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [message, setMessage] = useState(''),
    [selected, setSelected] = useState([]),
    [confirming, setConfirming] = useState(false),
    [tab, setTab] = useState('ready');
  const input = useRef(null),
    lock = useRef(false);
  const files = (batch?.files || []).filter((f) => f.state !== 'cancelled');
  const ready = files.filter((f) => f.state === 'ready');
  const groups = {
    ready,
    exception: files.filter((f) => f.state === 'exception'),
    progress: files.filter((f) => !['ready', 'exception'].includes(f.state)),
  };
  async function reload() {
    if (batch) setBatch(await api('/' + batch.id));
  }
  async function show() {
    setOpen(true);
    setError('');
    try {
      setHistory((await api('')).batches);
    } catch (e) {
      setError(e.message);
    }
  }
  useEffect(() => {
    if (!open || !batch) return;
    let alive = true;
    const timer = setInterval(async () => {
      if (document.hidden) return;
      try {
        const d = await api('/' + batch.id);
        if (alive) {
          setBatch(d);
          if (d.files.some((f) => f.state === 'success')) onUpdated?.();
        }
      } catch (e) {
        if (alive) setError(e.message);
      }
    }, 4000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [open, batch?.id]);
  async function uploadLocal(list) {
    if (lock.current || !list?.length) return;
    lock.current = true;
    setBusy(true);
    setError('');
    setConfirming(false);
    setSelected([]);
    try {
      const incoming = Array.from(list);
      if (incoming.length > 100) throw Error('每批最多 100 个文件');
      if (incoming.some((f) => !f.name.toLowerCase().endsWith('.pdf') || f.size > 5 * 1024 * 1024))
        throw Error('请选择 PDF，单个文件不超过 5 MB');
      const created = await api('', {});
      setBatch(created);
      for (let i = 0; i < incoming.length; i++) {
        setMessage(`正在识别 ${i + 1} / ${incoming.length}：${incoming[i].name}`);
        await api('/' + created.id + '/files', { file: incoming[i] });
        setBatch(await api('/' + created.id));
      }
      setMessage('识别完成。请逐项核对匹配结果，再勾选确认上传。');
      setTab('ready');
      setHistory((await api('')).batches);
    } catch (e) {
      setError(e.message);
    } finally {
      lock.current = false;
      setBusy(false);
      if (input.current) input.current.value = '';
    }
  }
  async function match(file, choice) {
    setError('');
    try {
      await api(`/${batch.id}/files/${file.id}/match`, choice ? { selection: JSON.parse(choice) } : {});
      await reload();
    } catch (e) {
      setError(e.message);
    }
  }
  async function cancel(file) {
    setError('');
    try {
      await api(`/${batch.id}/files/${file.id}/cancel`, {});
      setSelected((s) => s.filter((id) => id !== file.id));
      await reload();
    } catch (e) {
      setError(e.message);
    }
  }
  async function send() {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError('');
    try {
      const items = ready.filter((f) => selected.includes(f.id)).map((f) => ({ id: f.id, revision: f.revision }));
      setBatch(await api(`/${batch.id}/confirm`, { confirmed: true, items }));
      setConfirming(false);
      setSelected([]);
      setTab('progress');
      setMessage('已加入上传队列，成功结果将自动回查并更新订单开票状态。');
    } catch (e) {
      setError(e.message);
      setConfirming(false);
      await reload();
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }
  return (
    <>
      <NativeButton onClick={show}>批量导入 PDF</NativeButton>
      {open && (
        <AdminDialog>
          <section className="admin-dialog-content invoice-import-dialog" aria-label="批量导入发票 PDF">
            <div className="dialog-head">
              <div>
                <h2>批量导入发票 PDF</h2>
                <small>识别匹配 → 核对确认 → 上传并回查</small>
              </div>
              <NativeButton disabled={busy} aria-label="关闭" data-dialog-close onClick={() => setOpen(false)}>
                关闭
              </NativeButton>
            </div>
            <div className="invoice-import-toolbar">
              <TextInput
                ref={input}
                hidden
                type="file"
                accept="application/pdf,.pdf"
                multiple
                onChange={(e) => uploadLocal(e.target.files)}
              />
              <NativeButton className="primary" disabled={busy} onClick={() => input.current?.click()}>
                选择 PDF 新建批次
              </NativeButton>
              <Choice
                aria-label="历史导入批次"
                disabled={busy || confirming}
                value={batch?.id || ''}
                onChange={async (e) => {
                  if (!e.target.value) return;
                  try {
                    setBatch(await api('/' + e.target.value));
                    setSelected([]);
                    setMessage('');
                  } catch (err) {
                    setError(err.message);
                  }
                }}
              >
                <option value="">历史导入批次</option>
                {history.map((h) => (
                  <option key={h.id} value={h.id}>
                    {new Date(h.created * 1000).toLocaleString('zh-CN')} · {h.count} 个文件
                  </option>
                ))}
              </Choice>
              <NativeButton
                disabled={busy}
                onClick={async () => {
                  try {
                    await fetchJson('/api/invoices/refresh-pending', { method: 'POST' });
                    setMessage('已提交未开票补采，完成后点击“重新匹配”。');
                  } catch (e) {
                    setError(e.message);
                  }
                }}
              >
                同步未开票信息
              </NativeButton>
            </div>
            <p className="data-note">
              当前工作机器的拼多多店铺 · 原始电子普通发票 PDF，每个不超过 5
              MB。文件先保存至当前工作机器，确认后才上传至拼多多。扫描件、专票、红字票等单独列为异常。
            </p>
            {error && (
              <p className="error" role="alert">
                {error}
              </p>
            )}
            {message && (
              <p role="status" className="data-note">
                {message}
              </p>
            )}
            <div className="invoice-import-tabs">
              {[
                ['ready', '待确认'],
                ['exception', '异常项'],
                ['progress', '上传进度'],
              ].map(([key, title]) => (
                <NativeButton
                  key={key}
                  className={tab === key ? 'primary' : ''}
                  onClick={() => {
                    setTab(key);
                    setConfirming(false);
                  }}
                >
                  {title} {groups[key].length}
                </NativeButton>
              ))}
            </div>
            {confirming ? (
              <div className="invoice-confirm">
                <h3>确认向拼多多上传以下发票</h3>
                <p>请核对文件、店铺及订单。将按照下方对应关系提交，不会自动选择其他订单。</p>
                {ready
                  .filter((f) => selected.includes(f.id))
                  .map((f) => (
                    <div key={f.id}>
                      <strong>{f.name}</strong>
                      <span>
                        {f.selected.shop_name} · {f.selected.company_name}
                      </span>
                      <span>
                        订单 {f.selected.order_number} · {f.parsed.title_type} · {f.parsed.buyer_name} ·{' '}
                        {money(f.parsed.amount_cents)}
                      </span>
                      <span>发票号 {f.parsed.invoice_number}</span>
                    </div>
                  ))}
                <NativeButton disabled={busy} onClick={() => setConfirming(false)}>
                  返回核对
                </NativeButton>{' '}
                <NativeButton className="primary" disabled={busy} onClick={send}>
                  {busy ? '正在提交…' : '确认匹配无误，开始批量上传'}
                </NativeButton>
              </div>
            ) : (
              <>
                {tab === 'ready' && ready.length > 0 && (
                  <div className="invoice-import-toolbar">
                    <label>
                      <TextInput
                        type="checkbox"
                        checked={ready.length > 0 && ready.every((f) => selected.includes(f.id))}
                        onChange={(e) => setSelected(e.target.checked ? ready.map((f) => f.id) : [])}
                      />
                      全选待确认
                    </label>
                    <NativeButton
                      className="primary"
                      disabled={busy || !ready.some((f) => selected.includes(f.id))}
                      onClick={() => setConfirming(true)}
                    >
                      核对并上传 {ready.filter((f) => selected.includes(f.id)).length} 项
                    </NativeButton>
                  </div>
                )}
                <div className="invoice-import-table">
                  <SortableTable>
                    <thead>
                      <tr>
                        <th>选择 / 状态</th>
                        <th>PDF 与识别信息</th>
                        <th>匹配订单</th>
                        <th>操作 / 异常原因</th>
                      </tr>
                    </thead>
                    <tbody>
                      {groups[tab].map((f) => (
                        <tr key={f.id} className={f.state === 'exception' ? 'invoice-import-error' : ''}>
                          <td>
                            {f.state === 'ready' && (
                              <TextInput
                                type="checkbox"
                                aria-label={`选择 ${f.name}`}
                                checked={selected.includes(f.id)}
                                onChange={(e) =>
                                  setSelected((s) => (e.target.checked ? [...s, f.id] : s.filter((id) => id !== f.id)))
                                }
                              />
                            )}
                            <strong>{names[f.state]}</strong>
                          </td>
                          <td>
                            <a
                              href={`/api/invoice-imports/${batch.id}/files/${f.id}/pdf`}
                              target="_blank"
                              rel="noreferrer"
                            >
                              {f.name}
                            </a>
                            {f.parsed.invoice_number && (
                              <>
                                <span>
                                  发票 {f.parsed.invoice_number} · {f.parsed.invoice_date}
                                </span>
                                <span>
                                  {f.parsed.title_type} · {f.parsed.buyer_name} · {money(f.parsed.amount_cents)}
                                </span>
                                {f.parsed.buyer_tax && <span>购买方税号 {f.parsed.buyer_tax}</span>}
                                <span>销售方 {f.parsed.seller_name}</span>
                              </>
                            )}
                          </td>
                          <td>
                            {f.selected.id ? (
                              <>
                                <strong>{f.selected.shop_name}</strong>
                                <span>{f.selected.company_name}</span>
                                <span>{f.selected.order_number}</span>
                              </>
                            ) : (
                              <>
                                {f.candidates?.some((c) => !c.issues.length) && (
                                  <Choice
                                    aria-label={`选择订单 ${f.name}`}
                                    defaultValue=""
                                    onChange={(e) => e.target.value && match(f, e.target.value)}
                                  >
                                    <option value="">核对后选择订单</option>
                                    {f.candidates
                                      .filter((c) => !c.issues.length)
                                      .map((c) => (
                                        <option
                                          key={c.shop_id + ':' + c.id}
                                          value={JSON.stringify({ shop_id: c.shop_id, id: c.id })}
                                        >
                                          {c.shop_name} · {c.order_number} · {money(Math.round(c.amount * 100))}
                                        </option>
                                      ))}
                                  </Choice>
                                )}
                                {f.candidates
                                  ?.filter((c) => c.issues.length)
                                  .slice(0, 3)
                                  .map((c) => (
                                    <span key={c.shop_id + ':' + c.id}>
                                      {c.shop_name} · {c.order_number}：{c.issues.join('；')}
                                    </span>
                                  ))}
                              </>
                            )}
                          </td>
                          <td>
                            {f.error && <p>{f.error}</p>}
                            {['ready', 'exception'].includes(f.state) && (
                              <>
                                <NativeButton disabled={busy} onClick={() => match(f)}>
                                  重新匹配
                                </NativeButton>{' '}
                                <NativeButton disabled={busy} onClick={() => cancel(f)}>
                                  移除
                                </NativeButton>
                              </>
                            )}
                            {f.state === 'verify_pending' && <small>系统每分钟核验，不会重复提交。</small>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </SortableTable>
                  {!groups[tab].length && (
                    <p className="data-note">{batch ? '此分类暂无文件' : '选择 PDF 开始识别，或打开历史批次。'}</p>
                  )}
                </div>
              </>
            )}
          </section>
        </AdminDialog>
      )}
    </>
  );
}
