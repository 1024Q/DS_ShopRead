'use client';
import { ActionButton, TextInput, Choice } from '../shared/AdminControls';
import MultiSelect from '../shared/MultiSelect';
import { validSelection } from '../../lib/filter-scope.mjs';
import { useRef, useState } from 'react';
import AdminDialog from '../shared/AdminDialog';
import capabilities from '../../../config/date-collection.json';
import { shopLabel } from '../../lib/shop-identity.mjs';
const today = () => new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
export default function DateCollection({ kind, shops = [], platform = '', filters = {}, onCollected }) {
  const [open, setOpen] = useState(false),
    [module, setModule] = useState(kind || 'reviews'),
    [shop, setShop] = useState(''),
    [from, setFrom] = useState(''),
    [to, setTo] = useState(''),
    [period, setPeriod] = useState(''),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [message, setMessage] = useState('');
  const trigger = useRef(null),
    locked = useRef(false);
  const eligible = shops.filter((s) => (s.platform || 'pdd') === 'pdd'),
    config = capabilities[module],
    periodMode = ['month', 'year'].includes(config?.mode);
  const close = () => {
    if (locked.current) return;
    setOpen(false);
    trigger.current?.focus();
  };

  if ((kind && !capabilities[kind]) || (platform && platform !== 'pdd') || !eligible.length) return null;
  function show() {
    const selected = kind || 'reviews';
    setModule(selected);
    setShop(validSelection(filters.shop_id, eligible));
    setFrom(filters.date_from || today());
    setTo(filters.date_to || today());
    setPeriod(
      filters.period && !filters.period.includes('Q')
        ? filters.period
        : today().slice(0, capabilities[selected].mode === 'year' ? 4 : 7),
    );
    setError('');
    setMessage('');
    setOpen(true);
  }
  async function submit(e) {
    e.preventDefault();
    if (locked.current) return;
    setError('');
    if (!periodMode && (!from || !to || from > to || to > today())) {
      setError('请选择有效日期范围，结束日期不能晚于今天。');
      return;
    }
    if (!periodMode && config.mode !== 'catalog' && (Date.parse(to) - Date.parse(from)) / 86400000 >= 180) {
      setError('单次最多采集 180 天，请分段采集。');
      return;
    }
    locked.current = true;
    setBusy(true);
    try {
      const values = {
        shop_id: shop,
        platform: 'pdd',
        ...(periodMode ? { period } : { date_from: from, date_to: to, exact_dates: true }),
      };
      const params = new URLSearchParams(Object.entries({ ...values, force: true }).filter(([, v]) => v !== ''));
      const r = await fetch(`/api/data/${module}/sync?${params}`, { method: 'POST' });
      const d = await r.json();
      if (!r.ok) throw Error(typeof d.detail === 'string' ? d.detail : '日期采集提交失败');
      if (!d.tasks?.length) throw Error('当前没有可采集的店铺');
      setMessage(`已提交 ${d.tasks.length} 家店铺，完成后自动更新；未登录店铺等待登录。`);
      setOpen(false);
      onCollected?.(module, values);
    } catch (e) {
      setError(e.message);
    } finally {
      locked.current = false;
      setBusy(false);
    }
  }
  return (
    <div className="date-collection-control">
      <ActionButton type="button" ref={trigger} onClick={show}>
        采集指定日期
      </ActionButton>
      {message && <small role="status">{message}</small>}
      {open && (
        <AdminDialog label="采集指定日期">
          <section className="admin-dialog-content date-collection-dialog">
            <div className="dialog-head">
              <h2 id="date-collection-title">采集指定日期</h2>
              <ActionButton disabled={busy} onClick={close}>
                关闭
              </ActionButton>
            </div>
            <form onSubmit={submit}>
              <div className="filter-panel">
                <label>
                  采集模块
                  <Choice
                    autoFocus
                    disabled={busy || !!kind}
                    value={module}
                    onChange={(e) => {
                      setModule(e.target.value);
                      setPeriod(today().slice(0, capabilities[e.target.value].mode === 'year' ? 4 : 7));
                    }}
                  >
                    {Object.entries(capabilities).map(([key, c]) => (
                      <option key={key} value={key}>
                        {c.label}
                      </option>
                    ))}
                  </Choice>
                </label>
                <label>
                  采集店铺
                  <MultiSelect
                    label="店铺筛选"
                    value={shop}
                    onChange={(value) => setShop(value)}
                    options={eligible.map((s) => ({ value: s.id, label: shopLabel(s) }))}
                    placeholder="全部店铺"
                    disabled={busy}
                  />
                </label>
                {periodMode ? (
                  <label>
                    {config.dateLabel}
                    <TextInput
                      disabled={busy}
                      type={config.mode === 'year' ? 'number' : 'month'}
                      required
                      min={config.mode === 'year' ? '2000' : '2000-01'}
                      max={today().slice(0, config.mode === 'year' ? 4 : 7)}
                      value={period}
                      onChange={(e) => setPeriod(e.target.value)}
                    />
                  </label>
                ) : (
                  <>
                    <label>
                      {config.dateLabel}起
                      <TextInput
                        disabled={busy}
                        type="date"
                        required
                        max={to || today()}
                        value={from}
                        onChange={(e) => setFrom(e.target.value)}
                      />
                    </label>
                    <label>
                      {config.dateLabel}止
                      <TextInput
                        disabled={busy}
                        type="date"
                        required
                        min={from}
                        max={today()}
                        value={to}
                        onChange={(e) => setTo(e.target.value)}
                      />
                    </label>
                  </>
                )}
              </div>
              <p className="data-note">
                当前支持拼多多已接入模块。
                {config.note || '采集所选范围的全部状态，写入数据库；查询和排序仍读取数据库。'}
                {config.mode === 'dates' ? ' 单次最多 180 天，历史范围进入普通队列执行一次。' : ''}
                进度可在“同步与数据源”查看。
              </p>
              {error && (
                <p role="alert" className="error">
                  {error}
                </p>
              )}
              <div className="date-collection-actions">
                <ActionButton type="button" disabled={busy} onClick={close}>
                  取消
                </ActionButton>
                <ActionButton className="primary" disabled={busy} type="submit">
                  {busy ? '正在提交…' : '提交采集并查看'}
                </ActionButton>
              </div>
            </form>
          </section>
        </AdminDialog>
      )}
    </div>
  );
}
