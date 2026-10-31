'use client';
import { ActionButton, TextInput, Choice } from '../shared/AdminControls';
import { useEffect, useState } from 'react';
import DataTable from '../shared/DataTable';
import ReportShopPicker from './ReportShopPicker';
const money = (v) =>
  v === null || v === undefined
    ? '—'
    : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const states = {
  queued: '等待执行',
  querying: '读取月账单',
  downloading: '下载 / 接收',
  unpacking: '整理文件',
  completed: '已完成',
  exception: '需处理',
};
export default function PromotionLedger() {
  const [meta, setMeta] = useState(null),
    [data, setData] = useState(null),
    [selected, setSelected] = useState([]),
    [month, setMonth] = useState(''),
    [shop, setShop] = useState(''),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  async function api(path, body, method) {
    const r = await fetch('/api/' + path, {
      method: method || (body ? 'POST' : 'GET'),
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(20000),
    });
    const d = await r.json();
    if (!r.ok) throw Error(typeof d.detail === 'string' ? d.detail : '操作失败');
    return d;
  }
  async function load() {
    const [m, d] = await Promise.all([
      api('report-exports?kind=promotion'),
      api('promotion-ledger?' + new URLSearchParams({ month, shop_id: shop || 0 })),
    ]);
    setMeta(m);
    setData(d);
  }
  useEffect(() => {
    let alive = true,
      timer;
    async function poll() {
      try {
        const [m, d] = await Promise.all([
          api('report-exports?kind=promotion'),
          api('promotion-ledger?' + new URLSearchParams({ month, shop_id: shop || 0 })),
        ]);
        if (alive) {
          setMeta(m);
          setData(d);
        }
      } catch (e) {
        if (alive) setError(e.message);
      } finally {
        if (alive) timer = setTimeout(poll, 5000);
      }
    }
    poll();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [month, shop]);
  async function collect() {
    setBusy(true);
    setError('');
    try {
      await api('report-exports', { kind: 'promotion', shop_ids: selected, confirmed: true, regenerate: true });
      setSelected([]);
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  async function action(j, remove = false) {
    if (remove && !confirm('删除此推广账单任务、文件及导入数据？')) return;
    setBusy(true);
    try {
      await api(
        'report-exports/' + j.id + (remove ? '' : j.import_state === 'error' ? '/import' : '/retry'),
        remove ? undefined : {},
        remove ? 'DELETE' : 'POST',
      );
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  const jobs = (meta?.jobs || []).filter((j) => !shop || String(j.shop_id) === shop);
  return (
    <section>
      <header>
        <h1>多多推广流水</h1>
        <a href="/?view=profit">利润核算 →</a>
      </header>
      <p>
        从所属采集节点打开新标签页 → 财务流水 → 月账单 →
        下载明细。原文件保留在中心，导入平台提供的全部日期账单。已导入账单保留到手动删除。历史账单中平台未提供的金额显示“—”，不按零计算。
      </p>
      <div className="setup">
        <article>
          <h3>选择店铺</h3>
          <ReportShopPicker shops={meta?.shops || []} selected={selected} onChange={setSelected} disabled={busy} />
        </article>
        <article>
          <h3>采集说明</h3>
          <p>选择店铺即可下载并导入全部日期账单，无需设置月份。重复采集按店铺和月份展示最新成功版本，不重复累计。</p>
          <p>
            <ActionButton disabled={busy || !selected.length} onClick={collect}>
              采集全部日期账单（{selected.length} 家店铺）
            </ActionButton>
          </p>
          <p>
            月账单包括现金、红包、虚拟金等资金类型。推广消耗＝现金行的本月支出，按店铺和月份扣除一次；充值、红包和虚拟金不重复扣除。
          </p>
        </article>
      </div>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <article>
        <h3>采集进度</h3>
        <Choice value={shop} onChange={(e) => setShop(e.target.value)}>
          <option value="">全部店铺</option>
          {meta?.shops.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </Choice>
        <DataTable
          id="promotion-jobs"
          title="推广采集任务"
          rows={jobs}
          columns={[
            { id: 'shop_name', label: '店铺' },
            { id: 'node_name', label: '节点' },
            {
              id: 'status',
              label: '状态',
              value: (j) =>
                j.import_state === 'error'
                  ? '导入失败'
                  : j.import_state === 'ready'
                    ? '已导入'
                    : states[j.state] || j.state,
            },
            { id: 'message', label: '当前步骤', value: (j) => j.import_error || j.message },
            {
              id: 'actions',
              label: '操作',
              type: 'action',
              render: (j) => (
                <>
                  {j.state === 'completed' && <a href={'/api/report-exports/' + j.id + '/download'}>原文件</a>}
                  {(j.state === 'exception' || j.import_state === 'error') && (
                    <ActionButton disabled={busy} onClick={() => action(j)}>
                      重试
                    </ActionButton>
                  )}
                  {j.screenshot && j.state === 'exception' && (
                    <a href={'/api/report-exports/' + j.id + '/screenshot'} target="_blank" rel="noreferrer">
                      失败截图
                    </a>
                  )}
                  <ActionButton disabled={busy} onClick={() => action(j, true)}>
                    删除
                  </ActionButton>
                </>
              ),
            },
          ]}
        />
      </article>
      <article>
        <h3>月账单明细</h3>
        <label>
          查看月份（仅筛选展示）
          <TextInput type="month" value={month} onChange={(e) => setMonth(e.target.value)} />
        </label>
        <ActionButton onClick={() => setMonth('')}>全部月份</ActionButton>
        <p>{data?.notice}</p>
        <DataTable
          id="promotion-bills"
          title="推广月账单"
          query={{ month, shop }}
          rows={(data?.bills || []).flatMap((b) =>
            b.rows.map((r, i) => ({
              id: b.job_id + ':' + b.month + ':' + i,
              shop_name: b.shop_name,
              ...Object.fromEntries(r.map((v, k) => ['raw_' + k, v])),
            })),
          )}
          columns={[
            { id: 'shop_name', label: '店铺名称' },
            ...(data?.headers || []).map((h, i) => ({ id: 'raw_' + i, label: h, type: i >= 2 ? 'number' : 'text' })),
          ]}
        />
      </article>
      <style jsx>{`
        section {
          padding: 24px;
        }
        header {
          display: flex;
          align-items: center;
          justify-content: space-between;
        }
        .setup {
          display: grid;
          grid-template-columns: minmax(300px, 1fr) 2fr;
          gap: 18px;
        }
        article {
          padding: 22px;
          background: white;
          border: 1px solid #e4eaf3;
          border-radius: 14px;
          margin: 16px 0;
        }
        p {
          font-size: 13px;
          color: #77869b;
          line-height: 1.8;
        }
        input,
        select {
          margin: 8px;
          padding: 8px;
        }
        .wrap {
          overflow: auto;
        }
        table {
          border-collapse: collapse;
          width: 100%;
          font-size: 13px;
        }
        th,
        td {
          padding: 12px;
          text-align: left;
          border-bottom: 1px solid #e7edf5;
        }
        th {
          background: #f1f5fb;
        }
        button {
          margin: 4px;
        }
        .error {
          color: #b74232;
        }
        @media (max-width: 900px) {
          .setup {
            grid-template-columns: 1fr;
          }
        }
      `}</style>
    </section>
  );
}
