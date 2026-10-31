'use client';
import { ActionButton, TextInput, Choice } from '../shared/AdminControls';
import ReportAccounting from './ReportAccounting';
import { useEffect, useState } from 'react';
import DataTable from '../shared/DataTable';
export default function ReportView() {
  const [month, setMonth] = useState(''),
    [shop, setShop] = useState(''),
    [keyword, setKeyword] = useState(''),
    [search, setSearch] = useState(''),
    [status, setStatus] = useState(''),
    [meta, setMeta] = useState(null),
    [data, setData] = useState(null),
    [error, setError] = useState(''),
    [loading, setLoading] = useState(false),
    [ready, setReady] = useState(false),
    [revision, setRevision] = useState(0);
  useEffect(() => {
    const q = new URLSearchParams(location.search);
    setMonth(q.get('report_month') || '');
    setShop(q.get('report_shop') || '');
    setReady(true);
  }, []);
  useEffect(() => {
    const t = setTimeout(() => {
      setSearch(keyword);
    }, 300);
    return () => clearTimeout(t);
  }, [keyword]);
  useEffect(() => {
    if (!ready) return;
    let alive = true,
      timer;
    const controller = new AbortController();
    async function read() {
      if (document.hidden) {
        timer = setTimeout(read, 10000);
        return;
      }
      setLoading(true);
      try {
        const q = new URLSearchParams({ month, shop_id: shop || '0', status, metadata_only: 'true' });
        const responses = await Promise.all([
          fetch('/api/report-exports', { signal: controller.signal }),
          fetch('/api/report-exports/view?' + q, { signal: controller.signal }),
        ]);
        const [m, d] = await Promise.all(responses.map((r) => r.json()));
        if (!responses[0].ok || !responses[1].ok) throw Error(m.detail || d.detail || '读取失败');
        if (alive) {
          setMeta(m);
          setData(d);
          setError('');
        }
      } catch (e) {
        if (alive) setError(e.message);
      } finally {
        if (alive) {
          setLoading(false);
          timer = setTimeout(read, 10000);
        }
      }
    }
    read();
    return () => {
      alive = false;
      clearTimeout(timer);
      controller.abort();
    };
  }, [ready, month, shop, status, revision]);
  const shops = [...new Map((meta?.jobs || []).map((j) => [j.shop_id, { id: j.shop_id, name: j.shop_name }])).values()];
  function change(fn, value) {
    fn(value);
  }
  return (
    <section className="reports">
      <header>
        <h1>详细报表</h1>
        <div>
          <a href="/?view=profit_tax">税费设置</a> · <a href="/?view=profit">利润核算</a> ·{' '}
          <a href="/?view=cost_tables">成本表</a> · <a href="/?view=report_exports">生成报表</a>
        </div>
      </header>
      <div className="filters">
        <label>
          店铺
          <Choice value={shop} onChange={(e) => change(setShop, e.target.value)}>
            <option value="">全部店铺</option>
            {shops.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </Choice>
        </label>
        <label>
          月份
          <TextInput type="month" value={month} onChange={(e) => change(setMonth, e.target.value)} />
        </label>
        <label>
          订单状态
          <Choice value={status} onChange={(e) => change(setStatus, e.target.value)}>
            <option value="">全部状态</option>
            {data?.statuses.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </Choice>
        </label>
        <ActionButton
          onClick={() => {
            setMonth('');
            setShop('');
            setKeyword('');
            setStatus('');
          }}
        >
          重置
        </ActionButton>
      </div>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <p role="status">{loading ? '正在读取…' : `共 ${data?.total || 0} 条`}</p>
      <details className="admin-help">
        <summary>店铺月度核算与成本版本</summary>
        <ReportAccounting data={data} month={month} shop={shop} onChanged={() => setRevision((v) => v + 1)} />
      </details>
      <p>
        商品成本＝成本价×商品数量。快递费按成本版本中的计费方式核算；每单或每包裹多个SKU取最高费用一次。未匹配或收入口径未确认的行不计算毛利。税费按绑定版本分摊，分摊新增扣税已扣除原实收或费用中包含的税额；税后商品毛利不含推广及月度核对费用，完整税后利润请查看利润核算。
      </p>
      <details>
        <summary>报表来源及生成时间（{data?.reports.length || 0} 份，显示各范围最新成功版本）</summary>
        {data?.reports.map((r) => (
          <p key={r.id}>
            {r.shop_name} · {r.start} 至 {r.end} · {r.row_count} 行 · 生成于{' '}
            {new Date(r.created * 1000).toLocaleString('zh-CN')} ·{' '}
            <a href={'/api/report-exports/' + r.id + '/download'}>原文件</a>
          </p>
        ))}
      </details>
      <DataTable
        id="detailed-report"
        title="详细报表"
        source="orders"
        refreshKey={JSON.stringify([revision, data?.reports, data?.accounting])}
        query={{ month, shop_id: shop || 0, keyword: search, status }}
      />

      <style jsx>{`
        .reports {
          padding: 24px;
        }
        header,
        footer {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 16px;
        }
        h1 {
          font-size: 26px;
        }
        .filters {
          display: flex;
          gap: 14px;
          flex-wrap: wrap;
          align-items: end;
          background: white;
          padding: 18px;
          border: 1px solid #e4eaf3;
          border-radius: 12px;
        }
        .filters label {
          display: flex;
          flex-direction: column;
          gap: 7px;
          font-size: 13px;
        }
        .filters input,
        .filters select {
          padding: 9px;
          border: 1px solid #dce4ef;
          border-radius: 8px;
          min-width: 160px;
        }
        details {
          margin: 14px 0;
          font-size: 13px;
          color: #64748b;
        }
        footer {
          justify-content: flex-end;
          margin-top: 16px;
        }
      `}</style>
    </section>
  );
}
