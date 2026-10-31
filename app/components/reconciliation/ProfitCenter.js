'use client';
import { ConfigProvider } from 'antd';
import SortableTable from '../shared/SortableTable';
import { ActionButton, TextInput, Choice, TextArea } from '../shared/AdminControls';
import { useEffect, useState } from 'react';
import DataTable from '../shared/DataTable';
const money = (v) =>
  v == null ? '—' : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const kinds = {
  income_add: '补入售后等遗漏收入（加）',
  refund: '未反映退款（减）',
  cost_add: '补入遗漏商品成本（减）',
  cost_return: '退回可再售商品成本（加）',
  platform_fee: '平台费用（减）',
  other_fee: '其他费用（减）',
};
const checks = {
  scope: '已补齐报表排除的售后订单收入、商品成本，并核对实收口径',
  refunds: '已核对退款、退货及跨月售后，已反映的退款不再扣除',
  fees: '已核对平台及其他费用，实收已扣费用不再扣除',
  promotion: '已核对本月现金支出，未把充值、红包、虚拟金重复扣除',
  shipping: '已核对实际快递费，包含退货及补发费用',
};
async function api(path, body, method = 'GET') {
  const r = await fetch('/api/' + path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(30000),
  });
  const d = await r.json();
  if (!r.ok) throw Error(typeof d.detail === 'string' ? d.detail : '请求失败');
  return d;
}
function previousMonth() {
  const d = new Date();
  d.setDate(1);
  d.setMonth(d.getMonth() - 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}
export default function ProfitCenter() {
  const [month, setMonth] = useState(previousMonth),
    [shop, setShop] = useState(''),
    [items, setItems] = useState([]),
    [error, setError] = useState(''),
    [loading, setLoading] = useState(false),
    [refresh, setRefresh] = useState(0),
    [editing, setEditing] = useState(null);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError('');
    api('profit?' + new URLSearchParams({ month }))
      .then((d) => {
        if (alive) setItems(d.items);
      })
      .catch((e) => {
        if (alive) {
          setItems([]);
          setError(e.message);
        }
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [month, refresh]);
  const visible = items.filter((i) => !shop || String(i.shop_id) === shop);
  return (
    <section className="profit">
      <header>
        <h1>利润核算</h1>
        <nav>
          <a href="/?view=profit_tax">税费预估与核对</a> · <a href="/?view=report_view">详细报表 / 未匹配成本</a> ·{' '}
          <a href="/?view=promotion_ledger">推广账单</a>
        </nav>
      </header>
      <div className="filters">
        <label>
          核算月份
          <TextInput
            type="month"
            value={month}
            disabled={!!editing}
            onChange={(e) => {
              setMonth(e.target.value);
              setShop('');
            }}
          />
        </label>
        <label>
          店铺
          <Choice value={shop} disabled={!!editing} onChange={(e) => setShop(e.target.value)}>
            <option value="">全部店铺</option>
            {items.map((i) => (
              <option key={i.shop_id} value={i.shop_id}>
                {i.shop_name}
              </option>
            ))}
          </Choice>
        </label>
        <ActionButton disabled={loading || !!editing} onClick={() => setRefresh((v) => v + 1)}>
          刷新数据
        </ActionButton>
      </div>
      <details className="admin-help">
        <summary>利润计算与核对口径</summary>
        <article>
          <strong>月度经营利润＝实收金额 − 商品成本 − 快递费 − 推广现金支出 ＋ 核对调整</strong>
          <p>
            使用本月最新完整订单报表及其绑定的成本版本，推广账单按店铺、月份取最新版本。现金“本月支出”只扣一次，不扣充值、红包和虚拟金。已反映在实收、成本或推广支出中的调整项仅留档。
          </p>
          <p>
            现有订单报表范围为“无售后／取消售后”，未覆盖全部售后订单。预估利润不代表整店已核对利润；需补齐遗漏收入、退款、商品成本和费用。跨月退款按所选月份的核对口径登记，并在依据中说明。这是经营利润，未登记的人工等不在结果内。税费按报表绑定版本另列，实际税费替换预估值，已包含税费不重复扣。
          </p>
        </article>
      </details>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {loading && <p>正在读取核算数据…</p>}
      <DataTable
        id="profit"
        title="店铺月度利润"
        disabled={!!editing}
        query={{ month, shop }}
        rows={visible.map((i) => ({ ...i, id: i.shop_id + ':' + i.month }))}
        columns={[
          { id: 'shop_name', label: '店铺' },
          { id: 'month', label: '月份' },
          { id: 'book', label: '成本版本', value: (i) => i.calculation.book_id },
          { id: 'book_name', label: '成本表', value: (i) => i.calculation.book_name },
          ...Object.entries({
            revenue: '实收金额',
            product_cost: '商品成本',
            shipping: '快递费',
            promotion: '推广现金支出',
            adjustment: '核对调整',
            estimated: '税前预估利润',
            tax_estimated: '预估税费',
            tax_actual: '实际税费',
            tax_deduction: '新增扣税',
            after_tax: '税后预估利润',
          }).map(([id, label]) => ({ id, label, type: 'number', value: (i) => i.calculation[id] })),
          { id: 'checked_profit', label: '已核对税前利润', type: 'number' },
          { id: 'checked_tax_profit', label: '已核对税后利润', type: 'number' },
          { id: 'tax_version', label: '税费版本', value: (i) => i.calculation.tax_version },
          {
            id: 'state',
            label: '核对状态',
            value: (i) =>
              i.status === 'checked'
                ? '已核对'
                : i.status === 'stale'
                  ? '待重新核对'
                  : i.calculation.estimated == null
                    ? '数据不完整'
                    : '预估待核对',
          },
          { id: 'issues', label: '待处理事项', value: (i) => i.calculation.issues.join('；') },
          {
            id: 'actions',
            label: '操作',
            type: 'action',
            render: (i) => (
              <>
                <ActionButton disabled={!!editing || loading} onClick={() => setEditing(i)}>
                  核对明细
                </ActionButton>
                <a href={`/?view=report_view&report_month=${month}&report_shop=${i.shop_id}`}>报表明细</a>
              </>
            ),
          },
        ]}
      />

      {!loading && !visible.length && <p>所选月份尚无已导入报表或推广账单，请先导入。</p>}
      {editing && (
        <Review
          key={editing.shop_id + ':' + editing.month}
          item={editing}
          onClose={() => setEditing(null)}
          onSaved={(next) => {
            setItems((v) => v.map((i) => (i.shop_id === next.shop_id ? next : i)));
            setEditing(next);
          }}
        />
      )}
      <style jsx>{`
        .profit {
          padding: 24px;
          color: #293b53;
        }
        header,
        .filters {
          display: flex;
          align-items: center;
          gap: 18px;
          flex-wrap: wrap;
        }
        header {
          justify-content: space-between;
        }
        article {
          background: #f1f6ff;
          border: 1px solid #dce7f8;
          padding: 18px;
          border-radius: 12px;
          margin: 18px 0;
        }
        p {
          font-size: 13px;
          line-height: 1.8;
          color: #64748b;
        }
        .table-wrap {
          overflow: auto;
          background: white;
          border: 1px solid #e4eaf3;
          border-radius: 12px;
        }
        small {
          display: block;
          margin-top: 6px;
          color: #8592a5;
        }
        a {
          font-size: 13px;
        }
        .error {
          color: #b33c2d;
        }
        .warn {
          color: #ae6418;
        }
        .ok {
          color: #18836f;
        }
      `}</style>
    </section>
  );
}
function Review({ item, onClose, onSaved }) {
  const [draft, setDraft] = useState(item.draft),
    [dirty, setDirty] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  function change(patch) {
    setDraft((v) => ({ ...v, ...patch }));
    setDirty(true);
  }
  function line(index, patch) {
    change({ lines: draft.lines.map((x, i) => (i === index ? { ...x, ...patch } : x)) });
  }
  async function save(confirming = false) {
    setBusy(true);
    setError('');
    try {
      const next = await api(
        `profit/${item.shop_id}/${item.month}${confirming ? '/confirm' : ''}`,
        confirming ? { revision: item.revision, fingerprint: item.fingerprint } : { revision: item.revision, draft },
        confirming ? 'POST' : 'PUT',
      );
      setDraft(next.draft);
      setDirty(false);
      onSaved(next);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  const canConfirm =
    !dirty &&
    item.calculation.estimated != null &&
    draft.actual_shipping !== null &&
    draft.actual_shipping !== '' &&
    draft.evidence.trim() &&
    Object.values(checks).length === Object.values(draft.checks).filter(Boolean).length;
  return (
    <article className="review">
      <header>
        <h2>
          {item.shop_name} · {item.month} 月度核对
        </h2>
        <ActionButton
          disabled={busy}
          onClick={() => {
            if (!dirty || confirm('有未保存修改，确定关闭？')) onClose();
          }}
        >
          关闭
        </ActionButton>
      </header>
      <p>
        金额填写正数，系统按类型加减。凭据编号填写订单号／费用流水号。同类凭据重复会阻止保存。若一张凭据包含多项同类金额，请合并登记。平台费用与其他费用共享去重范围。
      </p>
      <ConfigProvider componentDisabled={busy}>
        <fieldset disabled={busy}>
          <label>
            当月实际快递费（填写后替代成本表预估快递费）
            <TextInput
              type="number"
              min="0"
              step="0.01"
              value={draft.actual_shipping ?? ''}
              onChange={(e) => change({ actual_shipping: e.target.value })}
              placeholder="未填写则使用预估"
            />
          </label>
          <div className="wrap">
            <SortableTable>
              <thead>
                <tr>
                  {['类型', '金额（元）', '凭据编号', '已包含在基础金额', '说明', '操作'].map((x) => (
                    <th key={x}>{x}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {draft.lines.map((x, i) => (
                  <tr key={i}>
                    <td>
                      <Choice value={x.kind} onChange={(e) => line(i, { kind: e.target.value })}>
                        {Object.entries(kinds).map(([k, v]) => (
                          <option key={k} value={k}>
                            {v}
                          </option>
                        ))}
                      </Choice>
                    </td>
                    <td>
                      <TextInput
                        aria-label="调整金额"
                        type="number"
                        min="0"
                        step="0.01"
                        value={x.amount}
                        onChange={(e) => line(i, { amount: e.target.value })}
                      />
                    </td>
                    <td>
                      <TextInput
                        aria-label="凭据编号"
                        value={x.reference}
                        maxLength={200}
                        onChange={(e) => line(i, { reference: e.target.value })}
                      />
                    </td>
                    <td>
                      <label>
                        <TextInput
                          type="checkbox"
                          checked={x.included}
                          onChange={(e) => line(i, { included: e.target.checked })}
                        />
                        仅留档，不重复计算
                      </label>
                    </td>
                    <td>
                      <TextInput
                        aria-label="说明"
                        value={x.note}
                        maxLength={1000}
                        onChange={(e) => line(i, { note: e.target.value })}
                      />
                    </td>
                    <td>
                      <ActionButton onClick={() => change({ lines: draft.lines.filter((_, n) => n !== i) })}>
                        移除
                      </ActionButton>
                    </td>
                  </tr>
                ))}
              </tbody>
            </SortableTable>
          </div>
          <ActionButton
            onClick={() =>
              change({
                lines: [...draft.lines, { kind: 'refund', amount: '', reference: '', note: '', included: false }],
              })
            }
          >
            添加退款 / 费用 / 成本核对项
          </ActionButton>
          <div className="checks">
            {Object.entries(checks).map(([k, v]) => (
              <label key={k}>
                <TextInput
                  type="checkbox"
                  checked={draft.checks[k] || false}
                  onChange={(e) => change({ checks: { ...draft.checks, [k]: e.target.checked } })}
                />
                {v}
              </label>
            ))}
          </div>
          <label>
            核对依据（账单文件名、凭据范围、跨月处理方式等）
            <TextArea
              value={draft.evidence}
              maxLength={3000}
              rows={3}
              onChange={(e) => change({ evidence: e.target.value })}
            />
          </label>
        </fieldset>
      </ConfigProvider>
      <p>
        当前保存版本税前利润：{money(item.calculation.estimated)} 元；税后利润：{money(item.calculation.after_tax)}{' '}
        元；税费版本：{item.calculation.tax_version || '未设置'}。
        {dirty
          ? '有未保存修改，请保存后查看更新结果。'
          : '确认后保存核对快照；报表、成本、税费或推广账单变化将要求重新核对。'}
      </p>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <ActionButton disabled={busy || !dirty} onClick={() => save()}>
        {busy ? '处理中…' : '保存核对草稿'}
      </ActionButton>
      <ActionButton disabled={busy || !canConfirm || item.status === 'checked'} onClick={() => save(true)}>
        确认已核对利润
      </ActionButton>
      {item.checked && (
        <details>
          <summary>上次核对记录 · {new Date(item.checked.at * 1000).toLocaleString('zh-CN')}</summary>
          <p>
            税前利润：{money(item.checked.calculation.estimated)} 元 · 税后利润：
            {money(item.checked.calculation.after_tax)} 元 · 成本版本：{item.checked.calculation.book_name || '无'}
          </p>
          <p>依据：{item.checked.draft.evidence}</p>
          <p>
            报表：{item.checked.source.report?.id} · 推广账单：{item.checked.source.promotion?.job_id}
          </p>
          {item.checked.draft.lines.map((x, i) => (
            <p key={i}>
              {kinds[x.kind]} · {money(x.amount)} 元 · {x.reference} · {x.included ? '已包含，不重复计算' : '计入调整'}{' '}
              · {x.note}
            </p>
          ))}
        </details>
      )}
      <style jsx>{`
        .review {
          margin-top: 24px;
          padding: 24px;
          background: white;
          border: 1px solid #a8c5ef;
          border-radius: 14px;
        }
        header {
          display: flex;
          justify-content: space-between;
          align-items: center;
        }
        h2 {
          font-size: 18px;
        }
        p {
          font-size: 13px;
          line-height: 1.8;
          color: #6b7b90;
        }
        fieldset {
          border: 0;
          padding: 0;
          min-width: 0;
        }
        .wrap {
          overflow: auto;
          margin-top: 16px;
        }
        .checks {
          display: grid;
          gap: 12px;
          margin: 22px 0;
          font-size: 14px;
        }
        .error {
          color: #b33c2d;
        }
        details {
          margin-top: 18px;
        }
      `}</style>
    </article>
  );
}
