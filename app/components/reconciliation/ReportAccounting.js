'use client';
import SortableTable from '../shared/SortableTable';
import { ActionButton, Choice } from '../shared/AdminControls';
import { useEffect, useState } from 'react';
const money = (v) =>
  v === null || v === undefined
    ? '—'
    : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export default function ReportAccounting({ data, month, shop, onChanged }) {
  const [missing, setMissing] = useState(null),
    [page, setPage] = useState(1),
    [open, setOpen] = useState(false),
    [busy, setBusy] = useState(''),
    [error, setError] = useState(''),
    [modes, setModes] = useState({});
  useEffect(() => {
    setPage(1);
    setMissing(null);
  }, [month, shop]);
  useEffect(() => {
    if (!open) return;
    let alive = true;
    fetch('/api/report-exports/unmatched?' + new URLSearchParams({ month, shop_id: shop || 0, page }))
      .then(async (r) => {
        const d = await r.json();
        if (!r.ok) throw Error(d.detail || '读取失败');
        if (alive) setMissing(d);
      })
      .catch((e) => {
        if (alive) setError(e.message);
      });
    return () => {
      alive = false;
    };
  }, [open, month, shop, page, data?.accounting?.map((s) => s.updated).join(',')]);
  async function calculate(s) {
    if (
      !confirm(
        '按当前启用成本表重新核算此报表？将替换此报表绑定的成本版本和收入口径。请先核对原文件金额是否为商品行金额或重复的整单金额。',
      )
    )
      return;
    setBusy(s.job_id);
    setError('');
    try {
      const r = await fetch('/api/report-exports/' + s.job_id + '/recalculate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirmed: true, income_mode: modes[s.job_id] || s.income_mode }),
      });
      const d = await r.json();
      if (!r.ok) throw Error(d.detail || '核算失败');
      onChanged();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy('');
    }
  }
  return (
    <article>
      <h3>店铺月度核算</h3>
      {!!data?.accounting_pending?.length && (
        <p>正在建立历史成本快照：{data.accounting_pending.join('、')}。完成后自动显示。</p>
      )}
      <p>
        汇总覆盖所选店铺、月份的完整报表，不随下方关键词或订单状态筛选变化。订单范围仍为“无售后／取消售后”。整单重复收入模式按商品数量分摊，并保持整单金额不变。毛利不等于完整财务净利润。
      </p>
      {data?.reports
        ?.filter((r) => data?.accounting_errors?.[r.id])
        .map((r) => (
          <p role="alert" key={r.id}>
            {r.shop_name} 核算失败：{data.accounting_errors[r.id]}{' '}
            <ActionButton disabled={!!busy} onClick={() => calculate({ job_id: r.id, income_mode: 'unverified' })}>
              重试核算
            </ActionButton>
          </p>
        ))}
      {error && <p role="alert">{error}</p>}
      <div style={{ overflowX: 'auto' }}>
        <SortableTable>
          <thead>
            <tr>
              {[
                '店铺 / 月份',
                '成本版本',
                '成本匹配',
                '实收金额',
                '商品成本',
                '预估快递费',
                '商品毛利',
                '扣运费后毛利',
                '税费版本 / 口径',
                '预估税费 / 实际税费',
                '新增扣税',
                '税后商品毛利',
                '核算设置',
              ].map((t) => (
                <th key={t}>{t}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data?.accounting?.map((s) => {
              const x = s.summary;
              return (
                <tr key={s.job_id}>
                  <td>
                    {x.shop_name}
                    <br />
                    {x.start.slice(0, 7)}
                  </td>
                  <td>{x.book_name || '未绑定成本表'}</td>
                  <td>
                    {x.matched} / {x.rows}
                    <br />
                    {!x.complete ? '未完整核算' : '成本已完整匹配'}
                    {x.ambiguous_income_rows > 0 && <div>有 {x.ambiguous_income_rows} 行收入口径待核验</div>}
                  </td>
                  <td>
                    {x.revenue_complete === false ? '待核验（已识别 ' + money(x.revenue) + '）' : money(x.revenue)}
                  </td>
                  <td>
                    {money(x.product_cost)}
                    {x.unmatched > 0 && '（已匹配部分）'}
                  </td>
                  <td>
                    {money(x.shipping)}
                    {!x.shipping_complete && '（不完整）'}
                  </td>
                  <td>
                    {money(x.profit)}
                    {!x.complete && (
                      <small>
                        已核算部分：{money(x.matched_profit)}
                        <br />
                        未匹配实收：{money(x.unmatched_revenue)}
                      </small>
                    )}
                  </td>
                  <td>{money(x.net)}</td>
                  <td>
                    {s.tax?.version_id?.slice(0, 8) || '未设置'}
                    <br />
                    {s.tax?.summary?.status}
                  </td>
                  <td>
                    {money(s.tax?.summary?.estimated)} / {money(s.tax?.summary?.actual)}
                  </td>
                  <td>{money(s.tax?.summary?.deduction)}</td>
                  <td>
                    {x.net != null && s.tax?.summary?.deduction != null
                      ? money(Number(x.net) - Number(s.tax.summary.deduction))
                      : '—'}
                  </td>
                  <td>
                    <Choice
                      aria-label="收入口径"
                      value={modes[s.job_id] || s.income_mode}
                      onChange={(e) => setModes((m) => ({ ...m, [s.job_id]: e.target.value }))}
                    >
                      <option value="unverified">自动核验（多行订单待确认）</option>
                      <option value="row">每行是商品实收金额</option>
                      <option value="order">每行重复整单实收金额</option>
                    </Choice>
                    <ActionButton disabled={!!busy} onClick={() => calculate(s)}>
                      {busy === s.job_id ? '核算中…' : '使用当前成本重新核算'}
                    </ActionButton>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </SortableTable>
      </div>
      <ActionButton onClick={() => setOpen((v) => !v)}>{open ? '收起' : '查看'}未匹配成本商品</ActionButton>
      {open && (
        <>
          <p>共 {missing?.total || 0} 行，可据此补充成本表，上传后主动重新核算历史报表。</p>
          <div style={{ overflowX: 'auto' }}>
            <SortableTable>
              <thead>
                <tr>
                  {['店铺', 'SKU编码', '商品', '规格', '订单号', '数量', '实收金额', '原因'].map((t) => (
                    <th key={t}>{t}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {missing?.rows.map((r, i) => (
                  <tr key={r.job_id + ':' + i}>
                    <td>{data?.accounting?.find((s) => s.job_id === r.job_id)?.summary.shop_name}</td>
                    <td>{r.cost.sku}</td>
                    <td>{r.cost.product}</td>
                    <td>{r.cost.spec}</td>
                    <td>{r.cost.order}</td>
                    <td>{r.cost.quantity}</td>
                    <td>{money(r.cost.amount)}</td>
                    <td>{r.cost.state}</td>
                  </tr>
                ))}
              </tbody>
            </SortableTable>
          </div>
          <ActionButton disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            上一页
          </ActionButton>{' '}
          {missing?.page || 1}/{missing?.pages || 1}{' '}
          <ActionButton disabled={page >= (missing?.pages || 1)} onClick={() => setPage((p) => p + 1)}>
            下一页
          </ActionButton>
        </>
      )}
      <style jsx>{`
        article {
          padding: 18px;
          background: white;
          border: 1px solid #e4eaf3;
          border-radius: 12px;
          margin: 16px 0;
        }
        p,
        small {
          font-size: 12px;
          color: #718096;
        }
        small {
          display: block;
        }
        select {
          max-width: 230px;
        }
      `}</style>
    </article>
  );
}
