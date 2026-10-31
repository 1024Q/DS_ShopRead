'use client';
import { uploadBody } from '../../lib/upload-body.mjs';
import SortableTable from '../shared/SortableTable';
import { ActionButton, TextInput, Choice } from '../shared/AdminControls';
import { useEffect, useState } from 'react';
import DataTable from '../shared/DataTable';
async function api(path, body) {
  const r = await fetch('/api/' + path, {
    method: body ? 'POST' : 'GET',
    ...(body ? uploadBody(body) : {}),
    signal: AbortSignal.timeout(30000),
  });
  const d = await r.json();
  if (!r.ok) throw Error(typeof d.detail === 'string' ? d.detail : '读取失败');
  return d;
}
export default function CostSupplements({ onSaved }) {
  const [data, setData] = useState({ items: [], jobs: [] }),
    [file, setFile] = useState(null),
    [preview, setPreview] = useState(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [notice, setNotice] = useState(''),
    [search, setSearch] = useState(''),
    [tick, setTick] = useState(0),
    [mode, setMode] = useState('piece'),
    [scope, setScope] = useState('current');
  useEffect(() => {
    let alive = true,
      timer;
    setData({ items: [], jobs: [] });
    async function load() {
      try {
        const d = await api('cost-supplements?' + new URLSearchParams({ scope }));
        if (alive) {
          setData(d);
          setError('');
        }
      } catch (e) {
        if (alive) setError(e.message);
      } finally {
        if (alive) timer = setTimeout(load, 10000);
      }
    }
    load();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [tick, scope]);
  async function choose(f) {
    setFile(null);
    setPreview(null);
    setError('');
    if (!f) return;
    if (f.size > 10 * 1024 * 1024) {
      setError('文件不能超过10MB');
      return;
    }
    setBusy(true);
    try {
      const body = {
        filename: f.name,
        file: f,
        sku_column: 0,
        cost_column: 3,
        shipping_column: 4,
        shipping_mode: mode,
      };
      const p = await api('cost-tables/preview', body);
      setFile(body);
      setPreview(p);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  async function save() {
    setBusy(true);
    setError('');
    try {
      const d = await api('cost-supplements', { ...file, shipping_mode: mode });
      setNotice(
        `已保存补充成本：${d.sku_count} 个SKU，关联 ${d.tasks.length} 份报表。后台自动重算，完成后利润同步更新。`,
      );
      setFile(null);
      setPreview(null);
      setTick((v) => v + 1);
      await onSaved();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  async function retry(id) {
    setBusy(true);
    try {
      await api('cost-supplements/' + id + '/retry', {});
      setTick((v) => v + 1);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  async function manual(scope) {
    const sku = window.prompt(
      `为 ${scope.shop_name || '此店铺'} 的商品填写商家编码：\n${scope.product}\n规格：${scope.spec}\n仅用于该店铺相同商品及规格的空SKU记录。`,
      scope.sku || '',
    );
    if (sku === null) return;
    setBusy(true);
    setError('');
    try {
      const job = await api('cost-supplements/manual-sku', {
        shop_id: scope.shop_id,
        product: scope.product,
        spec: scope.spec,
        sku,
        updated: scope.updated,
      });
      setNotice(`商家编码已保存，正在重新核算 ${job.tasks.length} 份报表及利润。`);
      setTick((v) => v + 1);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  const items = data.items.filter((r) =>
    [r.sku, ...r.products, ...r.specs, ...r.shops].join(' ').toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <article>
      <h3>未匹配成本与补充上传</h3>
      <p>
        默认按SKU汇总当前生效报表；可切换查看全部历史，来源列标明月份和报表版本。SKU不区分大小写。空SKU按商品、规格分开显示，可在操作栏按店铺手动填写商家编码，原始报表保留不变；暂不纳入上传模板。补差价收入不在此列表。
      </p>
      <div className="bar">
        <label>
          报表范围
          <Choice aria-label="未匹配成本报表范围" value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="current">当前生效报表</option>
            <option value="history">全部历史报表</option>
          </Choice>
        </label>
        <TextInput
          aria-label="搜索未匹配成本"
          placeholder="SKU / 商品 / 规格 / 店铺"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <a href={'/api/cost-supplements/template?' + new URLSearchParams({ scope })}>
          导出{scope === 'history' ? '全部历史' : '当前生效'}未匹配SKU补充模板（CSV）
        </a>
        <ActionButton onClick={() => setTick((v) => v + 1)}>刷新</ActionButton>
      </div>
      <p>
        共 {data.items.length} 组、{data.items.reduce((n, r) => n + r.rows, 0)} 行；筛选后 {items.length} 组。
      </p>
      <DataTable
        id="missing-cost"
        title="未匹配成本"
        query={{ scope }}
        rows={items.map((r) => ({ ...r, id: r.sku || JSON.stringify([r.products, r.specs]) }))}
        columns={[
          { id: 'sku', label: 'SKU', copy: true },
          { id: 'products', label: '商品名称', value: (r) => r.products.join('；') },
          { id: 'specs', label: '规格', value: (r) => r.specs.join('；') },
          { id: 'shops', label: '涉及店铺', value: (r) => r.shops.join('；') },
          {
            id: 'reports',
            label: '来源月份 / 报表版本',
            value: (r) =>
              (r.reports || [])
                .map(
                  (v) =>
                    `${v.shop_name} · ${v.month} · ${v.job_id.slice(0, 8)} · ${new Date(v.created * 1000).toLocaleString('zh-CN')}`,
                )
                .join('；'),
          },
          { id: 'rows', label: '报表行数', type: 'number' },
          { id: 'reasons', label: '原因', value: (r) => r.reasons.join('；') },
          {
            id: 'actions',
            label: '操作',
            type: 'action',
            render: (r) =>
              !r.sku &&
              (r.scopes || []).map((scope) => (
                <ActionButton key={scope.shop_id} disabled={busy} onClick={() => manual(scope)}>
                  {scope.shop_name} · 填写商家编码
                </ActionButton>
              )),
          },
        ]}
      />

      <details>
        <summary>手动商家编码记录（{data.sku_links?.length || 0}）</summary>
        {data.sku_links?.map((link) => (
          <div className="job" key={link.id}>
            <p>
              {link.shop_name} · {link.product} · {link.spec} → <strong>{link.sku}</strong> ·{' '}
              {new Date(link.updated * 1000).toLocaleString('zh-CN')}
            </p>
            <ActionButton disabled={busy} onClick={() => manual(link)}>
              修改编码并重新核算
            </ActionButton>
            {!!link.history?.length && <p>历史编码：{link.history.map((h) => h.sku).join(' → ')}</p>}
          </div>
        ))}
      </details>
      <p>
        编码保存后按报表绑定的成本版本匹配；若该版本仍缺少对应成本，将继续显示未匹配，可上传补充成本表。此对应关系也用于该店铺后续相同商品规格的空SKU记录。
      </p>
      <h3>补充成本表上传</h3>
      <p>
        下载模板后填写成本价、快递费，保留列顺序和SKU文本格式。只上传已填写完整的行；真实零费用填写0。保存仅补入缺失SKU，不覆盖已有价格。系统为受影响历史成本版本生成补充版本，保留其他SKU价格与快递费方式，自动重算对应报表及利润；已核对利润会提示重新核对。
      </p>
      <div className="bar">
        <label>
          快递费计费方式
          <Choice disabled={busy} value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="piece">每件</option>
            <option value="order">每单</option>
            <option value="package">每包裹</option>
          </Choice>
        </label>
        <TextInput
          aria-label="上传补充成本表"
          type="file"
          accept=".csv,.xlsx"
          disabled={busy}
          onChange={(e) => choose(e.target.files?.[0])}
        />
        <ActionButton disabled={busy || !preview?.valid} onClick={save}>
          {busy ? '处理中…' : '保存补充成本并自动重算'}
        </ActionButton>
      </div>
      {preview && (
        <>
          <p>
            {file?.filename} · {preview.total} 行 · {preview.valid ? '核验通过' : '请修正后重新上传'}
          </p>
          {preview.errors.map((x, i) => (
            <p className="error" key={i}>
              {x}
            </p>
          ))}
          <div className="wrap">
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
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {notice && <p role="status">{notice}</p>}
      <h3>补充成本处理进度</h3>
      {data.jobs.map((j) => (
        <div className="job" key={j.id}>
          <strong>{j.filename}</strong>
          <p>
            {j.state === 'completed' ? '已完成，报表及利润已更新' : j.state === 'error' ? '部分失败' : '排队 / 重算中'}{' '}
            · {j.tasks.filter((t) => t.state === 'completed').length} / {j.tasks.length} 份报表 ·{' '}
            {new Date(j.created * 1000).toLocaleString('zh-CN')}
          </p>
          {j.tasks
            .filter((t) => t.error)
            .map((t) => (
              <p className="error" key={t.job_id}>
                {t.shop_name || t.job_id}：{t.error}
              </p>
            ))}
          {j.state === 'error' && (
            <ActionButton disabled={busy} onClick={() => retry(j.id)}>
              重试失败报表
            </ActionButton>
          )}
        </div>
      ))}
      <style jsx>{`
        article {
          background: #fff;
          border: 1px solid #e3eaf3;
          border-radius: 14px;
          padding: 22px;
          margin: 18px 0;
        }
        h3 {
          margin: 20px 0 12px;
        }
        .bar {
          display: flex;
          gap: 14px;
          align-items: center;
          flex-wrap: wrap;
        }
        p {
          font-size: 13px;
          line-height: 1.8;
          color: #718096;
        }
        .wrap {
          overflow: auto;
          max-height: 420px;
        }
        td:nth-child(2),
        td:nth-child(3),
        td:nth-child(4) {
          min-width: 180px;
        }
        .error {
          color: #b94739;
        }
        .job {
          padding: 12px 0;
          border-top: 1px solid #edf1f7;
        }
      `}</style>
    </article>
  );
}
