'use client';
import { uploadBody } from '../../lib/upload-body.mjs';
import SortableTable from '../shared/SortableTable';
import { ActionButton, TextInput, Choice } from '../shared/AdminControls';
import { useEffect, useState } from 'react';
import DataTable from '../shared/DataTable';
import CostSupplements from './CostSupplements';
export default function CostTables() {
  const [data, setData] = useState({ books: [] }),
    [upload, setUpload] = useState(null),
    [preview, setPreview] = useState(null),
    [mapping, setMapping] = useState({ sku_column: '', cost_column: '', shipping_column: '', shipping_mode: 'piece' }),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState(''),
    [selected, setSelected] = useState('');
  async function api(path = '', body, method) {
    const r = await fetch('/api/cost-tables' + path, {
      method: method || (body ? 'POST' : 'GET'),
      cache: 'no-store',
      ...(body ? uploadBody(body) : {}),
    });
    const d = await r.json();
    if (!r.ok) throw Error(typeof d.detail === 'string' ? d.detail : '成本表操作失败');
    return d;
  }
  async function load() {
    const d = await api();
    setData(d);
    return d;
  }
  useEffect(() => {
    load().catch((e) => setMessage(e.message));
  }, []);
  function body(file = upload, map = mapping) {
    return {
      ...file,
      sku_column: map.sku_column === '' ? null : Number(map.sku_column),
      cost_column: map.cost_column === '' ? null : Number(map.cost_column),
      shipping_column: map.shipping_column === '' ? null : Number(map.shipping_column),
      shipping_mode: map.shipping_mode,
    };
  }
  async function choose(file) {
    if (!file) return;
    setPreview(null);
    setUpload(null);
    setMessage('');
    if (file.size > 10 * 1024 * 1024) {
      setMessage('文件不能超过10MB');
      return;
    }
    setBusy(true);
    try {
      const u = { filename: file.name, file };
      const p = await api('/preview', u);
      setUpload(u);
      setPreview(p);
      setMapping({
        sku_column: p.mapping.sku_column ?? '',
        cost_column: p.mapping.cost_column ?? '',
        shipping_column: p.mapping.shipping_column ?? '',
        shipping_mode: p.mapping.shipping_mode || 'piece',
      });
    } catch (e) {
      setMessage(e.message);
    } finally {
      setBusy(false);
    }
  }
  async function verify() {
    setBusy(true);
    try {
      setPreview(await api('/preview', body()));
      setMessage('');
    } catch (e) {
      setMessage(e.message);
    } finally {
      setBusy(false);
    }
  }
  async function save() {
    if (
      !window.confirm('保存并启用此成本表？新导入报表使用此版本，历史报表保留已绑定版本，可在详细报表中手动重新核算。')
    )
      return;
    setBusy(true);
    try {
      const d = await api('', body());
      await load();
      setSelected(d.id);
      setUpload(null);
      setPreview(null);
      setMessage(`已启用成本表，共 ${d.count} 条SKU成本。`);
    } catch (e) {
      setMessage(e.message);
    } finally {
      setBusy(false);
    }
  }
  async function action(book, remove = false) {
    if (
      !window.confirm(
        remove
          ? `删除成本表“${book.name}”？${book.id === data.active_id ? '当前表启用中，删除后新报表将无默认成本表。已绑定历史报表的版本不可删除。' : ''}`
          : `启用“${book.name}”作为全部店铺的SKU成本表？`,
      )
    )
      return;
    setBusy(true);
    try {
      await api('/' + book.id + (remove ? '' : '/activate'), remove ? undefined : {}, remove ? 'DELETE' : 'POST');
      if (remove && selected === book.id) setSelected('');
      await load();
      setMessage(remove ? '成本表已删除' : '成本表已启用，历史报表成本版本保持不变');
    } catch (e) {
      setMessage(e.message);
    } finally {
      setBusy(false);
    }
  }
  const mapped =
    preview &&
    preview.mapping.sku_column === Number(mapping.sku_column) &&
    preview.mapping.cost_column === Number(mapping.cost_column) &&
    mapping.sku_column !== '' &&
    mapping.cost_column !== '' &&
    (preview.mapping.shipping_column ?? '') ===
      (mapping.shipping_column === '' ? '' : Number(mapping.shipping_column)) &&
    preview.mapping.shipping_mode === mapping.shipping_mode;
  return (
    <section className="costs">
      <header>
        <div>
          <h1>成本表</h1>
          <p>按商家编码－规格维度（SKU）精确匹配全部店铺</p>
        </div>
        <a href="/?view=report_view">查看报表成本与毛利 →</a>
      </header>
      <article>
        <h3>上传成本表</h3>
        <p>
          支持 CSV、单工作表
          XLSX，第一行是列名，最多10MB、50,000行。上传后选择SKU列和单位成本列（元/件）。SKU请使用文本格式，保留前导零。
        </p>
        <TextInput
          aria-label="上传成本表"
          type="file"
          accept=".csv,.xlsx"
          disabled={busy}
          onChange={(e) => choose(e.target.files?.[0])}
        />
        <a
          className="template"
          download="成本表模板.csv"
          href={
            'data:text/csv;charset=utf-8,' +
            encodeURIComponent('\uFEFF商家编码,商品名称,成本价,快递费\n示例SKU001,示例商品,12.50,3.00\n')
          }
        >
          下载CSV模板
        </a>
        {preview && (
          <>
            <div className="mapping">
              {[
                ['sku_column', 'SKU编码列'],
                ['cost_column', '单位成本列（元/件）'],
                ['shipping_column', '快递费列（可选）'],
              ].map(([key, label]) => (
                <label key={key}>
                  {label}
                  <Choice
                    value={mapping[key]}
                    disabled={busy}
                    onChange={(e) => setMapping((m) => ({ ...m, [key]: e.target.value }))}
                  >
                    <option value="">请选择</option>
                    {preview.headers.map((h, i) => (
                      <option key={i} value={i}>
                        {i + 1}. {h || '空列名'}
                      </option>
                    ))}
                  </Choice>
                </label>
              ))}
              <label>
                快递费计费方式
                <Choice
                  disabled={busy}
                  value={mapping.shipping_mode}
                  onChange={(e) => setMapping((m) => ({ ...m, shipping_mode: e.target.value }))}
                >
                  <option value="piece">每件</option>
                  <option value="order">每单（多个SKU取最高费一次）</option>
                  <option value="package">每包裹（多个SKU取最高费一次）</option>
                </Choice>
              </label>
              <ActionButton disabled={busy} onClick={verify}>
                核验列与数据
              </ActionButton>
              <ActionButton className="primary" disabled={busy || !preview.valid || !mapped} onClick={save}>
                保存并启用
              </ActionButton>
            </div>
            <p>
              {upload.filename} · {preview.total} 行 · 预览前10行{!mapped ? ' · 列已调整，请重新核验' : ''}
            </p>
            {preview.errors.length > 0 && (
              <div role="alert" className="errors">
                共 {preview.error_count} 项问题，请修正后上传：
                {preview.errors.map((e, i) => (
                  <div key={i}>{e}</div>
                ))}
              </div>
            )}
            <div className="table-wrap">
              <SortableTable>
                <thead>
                  <tr>
                    {preview.headers.map((h, i) => (
                      <th key={i}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {preview.sample.map((r, i) => (
                    <tr key={i}>
                      {r.map((v, k) => (
                        <td key={k}>{v}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </SortableTable>
            </div>
          </>
        )}
      </article>
      <CostSupplements onSaved={load} />
      {message && (
        <p role="status" className="message">
          {message}
        </p>
      )}
      <article>
        <h3>已上传的成本表</h3>
        <p>
          同一时间启用一份通用成本表。启用新表只影响新导入报表；历史报表绑定原版本。快递费为预估费用，未提供时不按零计算。
        </p>
        {!data.books.length && <p>尚未上传成本表</p>}
        {data.books.map((b) => (
          <div className="book" key={b.id}>
            <div>
              <strong>{b.name}</strong> {b.id === data.active_id && <span className="active">使用中</span>}
              <small>
                {b.count} 行 · {new Date(b.created * 1000).toLocaleString('zh-CN')}
              </small>
            </div>
            <div>
              <ActionButton
                disabled={busy}
                onClick={() => {
                  setSelected(b.id);
                }}
              >
                查看
              </ActionButton>
              {b.id !== data.active_id && (
                <ActionButton disabled={busy} onClick={() => action(b)}>
                  启用
                </ActionButton>
              )}
              <ActionButton className="delete" disabled={busy} onClick={() => action(b, true)}>
                删除
              </ActionButton>
            </div>
          </div>
        ))}
      </article>
      {selected && (
        <article>
          <h3>成本表明细</h3>
          <DataTable key={selected} id="cost-detail" title="成本表明细" costId={selected} />
        </article>
      )}
      <style jsx>{`
        .costs {
          padding: 24px;
          max-width: 1700px;
          margin: auto;
        }
        header,
        .mapping,
        .book,
        footer {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 14px;
          flex-wrap: wrap;
        }
        h1 {
          font-size: 26px;
        }
        h3 {
          margin: 0 0 12px;
        }
        p,
        small {
          color: #7a889d;
          font-size: 13px;
          line-height: 1.7;
        }
        article {
          background: white;
          border: 1px solid #e3eaf3;
          border-radius: 14px;
          padding: 22px;
          margin: 18px 0;
        }
        .mapping {
          justify-content: flex-start;
          align-items: end;
          margin: 18px 0;
        }
        .mapping label {
          display: flex;
          flex-direction: column;
          gap: 8px;
          font-size: 13px;
        }
        select,
        input {
          padding: 9px;
          border: 1px solid #dce4ef;
          border-radius: 8px;
          background: white;
        }
        .template {
          margin-left: 20px;
          font-size: 13px;
          color: #3577db;
        }
        .table-wrap {
          overflow: auto;
          max-height: 450px;
        }
        .errors {
          padding: 12px;
          background: #fff2ee;
          color: #b94739;
          font-size: 13px;
        }
        .book {
          padding: 14px 0;
          border-bottom: 1px solid #edf1f7;
        }
        .book small {
          display: block;
        }
        .book button {
          margin-left: 8px;
        }
        .active {
          background: #e9f7f1;
          color: #21896a;
          padding: 4px 8px;
          border-radius: 6px;
          font-size: 12px;
        }
        .delete {
          color: #b94739;
        }
        .message {
          padding: 12px;
          background: #eef5ff;
          border-radius: 8px;
        }
        footer {
          justify-content: flex-end;
          margin-top: 16px;
          font-size: 13px;
        }
      `}</style>
    </section>
  );
}
