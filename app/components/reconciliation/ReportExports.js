'use client';
import AdminDialog from '../shared/AdminDialog';
import { ActionButton, TextInput, Choice } from '../shared/AdminControls';
import { useEffect, useState } from 'react';
import DataTable from '../shared/DataTable';
import { deleteSelectedReports, retainReportSelection } from '../../lib/report-delete.mjs';
import { filterReportJobs, reportStorage, formatBytes } from '../../lib/report-task-tools.mjs';
import ReportTaskActions from './ReportTaskActions';
import ReportShopPicker from './ReportShopPicker';
const states = {
  queued: '等待执行',
  querying: '查询订单',
  configuring: '配置字段',
  submitting: '提交生成',
  generating: '平台生成中',
  downloading: '下载 / 接收',
  unpacking: '解压命名',
  completed: '已完成',
  exception: '需处理',
};
const clock = (v) => (v ? new Date(v * 1000).toLocaleString('zh-CN', { hour12: false }) : '—');
export default function ReportExports() {
  const [data, setData] = useState(null),
    [selected, setSelected] = useState([]),
    [month, setMonth] = useState(() => {
      const d = new Date();
      d.setDate(1);
      d.setMonth(d.getMonth() - 1);
      return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
    }),
    [regenerate, setRegenerate] = useState(false),
    [message, setMessage] = useState(''),
    [busy, setBusy] = useState(false),
    [confirm, setConfirm] = useState(false),
    [days, setDays] = useState(7);
  const [selectedJobs, setSelectedJobs] = useState([]),
    [deleteResults, setDeleteResults] = useState([]);
  const [taskFilters, setTaskFilters] = useState({ shop: '', month: '', state: '' });
  const allJobs = data?.jobs || [],
    jobs = filterReportJobs(allJobs, taskFilters),
    selectedJobSet = new Set(selectedJobs),
    selectedReports = jobs.filter((j) => selectedJobSet.has(j.id)),
    storage = reportStorage(selectedReports),
    someJobsSelected = selectedReports.length > 0;
  const reportShops = [...new Map(allJobs.map((j) => [String(j.shop_id), j.shop_name])).entries()];
  useEffect(() => {
    setSelectedJobs((v) => retainReportSelection(v, filterReportJobs(data?.jobs || [], taskFilters)));
  }, [data, taskFilters]);
  const filterTasks = (key, value) => {
    setTaskFilters((v) => ({ ...v, [key]: value }));
    setSelectedJobs([]);
  };
  async function importAgain(id) {
    try {
      await request('/' + id + '/import', { method: 'POST' });
      await load();
    } catch (e) {
      setMessage(e.message);
    }
  }

  const [year, mo] = month.split('-').map(Number);
  const start = month ? month + '-01T00:00:00' : '';
  const end = month
    ? `${mo === 12 ? year + 1 : year}-${String(mo === 12 ? 1 : mo + 1).padStart(2, '0')}-01T00:00:00`
    : '';
  async function request(path = '', options = {}) {
    const r = await fetch('/api/report-exports' + path, {
      cache: 'no-store',
      ...options,
      headers: { 'Content-Type': 'application/json' },
      signal: AbortSignal.timeout(20000),
    });
    const d = await r.json();
    if (!r.ok) {
      const error = Error(d.detail || '操作失败');
      error.status = r.status;
      throw error;
    }
    return d;
  }
  async function load() {
    try {
      const d = await request();
      setData(d);
      setSelectedJobs((v) => retainReportSelection(v, d.jobs));
      setSelected((v) => v.filter((id) => d.shops.some((s) => s.id === id && s.online)));
    } catch (e) {
      setMessage(e.message);
    }
  }
  useEffect(() => {
    let alive = true,
      initial = true,
      loading = false;
    const read = async () => {
      if (loading) return;
      loading = true;
      try {
        const d = await request();
        if (alive) {
          setData(d);
          setSelectedJobs((v) => retainReportSelection(v, d.jobs));
          setSelected((v) => v.filter((id) => d.shops.some((s) => s.id === id && s.online)));
          if (initial) {
            setDays(d.retention_days);
            initial = false;
          }
        }
      } catch (e) {
        if (alive) setMessage('自动更新暂不可用：' + e.message);
      } finally {
        loading = false;
      }
    };
    const visible = () => {
      if (!document.hidden) read();
    };
    read();
    const t = setInterval(visible, 2000);
    document.addEventListener('visibilitychange', visible);
    window.addEventListener('focus', visible);
    return () => {
      alive = false;
      clearInterval(t);
      document.removeEventListener('visibilitychange', visible);
      window.removeEventListener('focus', visible);
    };
  }, []);
  async function submit() {
    setBusy(true);
    try {
      await request('', {
        method: 'POST',
        body: JSON.stringify({ shop_ids: selected, month, regenerate, confirmed: true }),
      });
      setConfirm(false);
      setMessage('已处理：已有报表直接复用，新任务完成后自动导入，可在报表查看中查询。');
      await load();
    } catch (e) {
      setMessage(e.message);
    } finally {
      setBusy(false);
    }
  }
  async function retry(id, restart = false) {
    if (
      restart &&
      !window.confirm('请确认拼多多已生成报表中没有本次报表。继续将重新提交生成，若原报表实际存在可能重复生成。')
    )
      return;
    setBusy(true);
    try {
      await request('/' + id + '/retry', { method: 'POST', body: JSON.stringify({ restart_submission: restart }) });
      setMessage(restart ? '已确认未生成，等待重新提交。' : '已提交恢复；已提交平台的任务只核验和下载原报表。');
      await load();
    } catch (e) {
      setMessage(e.message);
    } finally {
      setBusy(false);
    }
  }
  async function deleteReports(targets) {
    if (busy || !targets.length) return;
    const items = [...new Map(targets.map((j) => [j.id, j])).values()];
    const preview = items
      .slice(0, 6)
      .map((j) => `${j.shop_name} · ${j.start} 至 ${j.end}`)
      .join('\n');
    if (
      !window.confirm(
        `确认删除选中的 ${items.length} 份报表？\n${preview}${items.length > 6 ? '\n……' : ''}\n将同时删除报表文件、失败截图、任务记录及对应数据库报表数据；正在执行的任务会先取消。`,
      )
    )
      return;
    setBusy(true);
    setDeleteResults([]);
    setMessage(`正在删除 0 / ${items.length} 份报表…`);
    try {
      const results = await deleteSelectedReports(
        items,
        (j) => request('/' + j.id, { method: 'DELETE' }),
        (result, done, total) => {
          setDeleteResults((v) => [...v, result]);
          setMessage(`正在删除 ${done} / ${total} 份报表…`);
          if (result.ok) {
            setData((d) => (d ? { ...d, jobs: d.jobs.filter((j) => j.id !== result.id) } : d));
            setSelectedJobs((v) => v.filter((id) => id !== result.id));
          }
        },
      );
      await load();
      const failed = results.filter((r) => !r.ok),
        pending = results.filter((r) => r.pending).length;
      setSelectedJobs(failed.map((r) => r.id));
      setMessage(
        `删除完成：成功 ${results.length - failed.length} 份，失败 ${failed.length} 份。${pending ? `其中 ${pending} 份中心数据已清理，节点文件等待连接或更新后自动清理。` : ''}${failed.length ? '失败项已保留，可重试。' : ''}`,
      );
    } finally {
      setBusy(false);
    }
  }
  async function saveDays() {
    setBusy(true);
    try {
      await request('/settings', { method: 'PUT', body: JSON.stringify({ retention_days: Number(days) }) });
      setMessage('节点文件保留时间已保存；中心已导入报表保留到手动删除');
    } catch (e) {
      setMessage(e.message);
    } finally {
      setBusy(false);
    }
  }
  const valid =
    selected.length > 0 &&
    selected.length <= 50 &&
    start &&
    end &&
    start <= end &&
    selected.every((id) => data?.shops.some((s) => s.id === id && s.online));
  return (
    <section className="report-export">
      <header>
        <div>
          <h1>报表导出</h1>
          <p>全部采集节点 · 拼多多店铺 · 手动执行</p>
        </div>
        <span className={data?.online ? 'online' : 'offline'}>
          {data ? `${data.online_nodes} / ${data.total_nodes} 台节点在线` : '正在读取节点状态'}
        </span>
      </header>
      <div className="export-layout">
        <div className="export-card">
          <h3>
            选择店铺 <small>{selected.length} 家</small>
          </h3>
          <ReportShopPicker shops={data?.shops || []} selected={selected} onChange={setSelected} disabled={busy} />
        </div>
        <div className="export-card">
          <h3>导出条件</h3>
          <div className="export-dates">
            <label>
              导出月份
              <TextInput type="month" value={month} onChange={(e) => setMonth(e.target.value)} />
            </label>
            <label>
              订单下单时间（北京时间）
              <span>
                {start.replace('T', ' ')} 至 {end.replace('T', ' ')}
              </span>
            </label>
          </div>
          <p>选择近90天范围内的完整月份，时间自动换算为月初至下月月初。平台筛选边界保持原样。</p>
          <label>
            <TextInput type="checkbox" checked={regenerate} onChange={(e) => setRegenerate(e.target.checked)} />{' '}
            重新生成（成功导入后替换旧版展示）
          </label>
          <dl>
            <dt>订单范围</dt>
            <dd>全部订单（近 3 个月）</dd>
            <dt>售后状态</dt>
            <dd>无售后 / 取消售后</dd>
            <dt>订单状态</dt>
            <dd>全部</dd>
            <dt>报表类型</dt>
            <dd>自定义报表</dd>
          </dl>
          <div className="export-fields">
            {data?.fields.map((f) => (
              <span key={f}>{f}</span>
            ))}
          </div>
          <ActionButton className="primary" disabled={!valid || busy || !data?.online} onClick={() => setConfirm(true)}>
            核对并生成报表
          </ActionButton>
        </div>
      </div>
      {message && (
        <p role="status" className="export-message">
          {message}
        </p>
      )}
      <div className="export-card">
        <div className="export-heading">
          <h3>
            导出任务 <small>每2秒自动更新 · {clock(data?.refreshed_at)}</small>
          </h3>
          <div className="export-retention">
            <label>
              节点保留{' '}
              <TextInput
                aria-label="文件保留天数"
                type="number"
                min="1"
                max="30"
                value={days}
                onChange={(e) => setDays(e.target.value)}
              />{' '}
              天
            </label>
            <ActionButton disabled={busy} onClick={saveDays}>
              保存
            </ActionButton>
            <ActionButton onClick={load}>刷新</ActionButton>
          </div>
        </div>
        <p>
          同店生成间隔至少 5 分钟。平台记录保留 24
          小时，完成后文件自动接收到中心；压缩包含重命名后的表格及原始压缩文件。中心已导入报表与成本快照保留到手动删除。
        </p>
        <div className="task-filters" aria-label="导出任务筛选">
          <label>
            店铺
            <Choice
              aria-label="筛选报表店铺"
              value={taskFilters.shop}
              disabled={busy}
              onChange={(e) => filterTasks('shop', e.target.value)}
            >
              <option value="">全部店铺</option>
              {reportShops.map(([id, name]) => (
                <option key={id} value={id}>
                  {name}
                </option>
              ))}
            </Choice>
          </label>
          <label>
            导出月份
            <TextInput
              aria-label="筛选报表月份"
              type="month"
              value={taskFilters.month}
              disabled={busy}
              onChange={(e) => filterTasks('month', e.target.value)}
            />
          </label>
          <label>
            任务状态
            <Choice
              aria-label="筛选报表状态"
              value={taskFilters.state}
              disabled={busy}
              onChange={(e) => filterTasks('state', e.target.value)}
            >
              <option value="">全部状态</option>
              {Object.entries(states).map(([id, name]) => (
                <option key={id} value={id}>
                  {name}
                </option>
              ))}
            </Choice>
          </label>
          <ActionButton
            disabled={busy}
            onClick={() => {
              setTaskFilters({ shop: '', month: '', state: '' });
              setSelectedJobs([]);
            }}
          >
            重置筛选
          </ActionButton>
        </div>
        <div className="selection-storage" aria-live="polite">
          <strong>
            已选 {selectedReports.length} 份 · 报表文件 {formatBytes(storage.center + storage.node)}
          </strong>
          <span>
            中心 {formatBytes(storage.center)} · 节点最近回报 {formatBytes(storage.node)}
            {storage.unknown ? ` · ${storage.unknown} 份节点占用未回报` : ''} · 数据库 {storage.rows.toLocaleString()}{' '}
            行
          </span>
          <small>空间按报表文件、原始下载及截图统计；数据库记录删除后不等于立即释放相同磁盘空间。</small>
        </div>
        <div className="export-actions">
          <span>
            筛选结果 {jobs.length} / {allJobs.length} 份
          </span>
          <ActionButton
            className="batch-delete"
            disabled={busy || !someJobsSelected}
            onClick={() => deleteReports(jobs.filter((j) => selectedJobSet.has(j.id)))}
          >
            批量删除（{selectedReports.length}）
          </ActionButton>
        </div>
        <DataTable
          id="report-tasks"
          title="报表导出任务"
          query={taskFilters}
          rows={jobs}
          selectedIds={selectedJobs}
          onSelectionChange={setSelectedJobs}
          disabled={busy}
          columns={[
            { id: 'shop_name', label: '店铺' },
            { id: 'node_name', label: '节点' },
            { id: 'start', label: '起始时间' },
            { id: 'end', label: '结束时间' },
            {
              id: 'created',
              label: '生成时间',
              value: (j) => new Date(j.created * 1000).toISOString(),
              render: (j) => clock(j.created),
            },
            { id: 'state', label: '状态', value: (j) => states[j.state] || j.state },
            { id: 'message', label: '处理说明', value: (j) => j.import_error || j.message },
            { id: 'center_file_bytes', label: '中心文件字节', type: 'number' },
            { id: 'node_file_bytes', label: '节点文件字节', type: 'number' },
            {
              id: 'actions',
              label: '操作',
              type: 'action',
              render: (j) => (
                <>
                  <ReportTaskActions
                    job={j}
                    busy={busy}
                    onRetry={retry}
                    onDelete={deleteReports}
                    onImport={importAgain}
                  />
                  {j.state === 'exception' && j.screenshot && (
                    <a href={'/api/report-exports/' + j.id + '/screenshot'} target="_blank" rel="noreferrer">
                      失败截图
                    </a>
                  )}
                </>
              ),
            },
          ]}
        />
      </div>

      {deleteResults.length > 0 && (
        <div className="export-card" aria-label="批量删除结果">
          <h3>删除结果</h3>
          {deleteResults.map((r) => (
            <p key={r.id} className={r.ok ? 'online' : 'offline'}>
              {r.shop_name} · {r.start} 至 {r.end}：
              {r.ok
                ? r.pending
                  ? '中心数据已删除，节点文件待清理'
                  : r.alreadyDeleted
                    ? '报表已删除'
                    : '报表文件及数据库数据已删除'
                : r.error}
            </p>
          ))}
          {deleteResults.some((r) => !r.ok) && (
            <ActionButton disabled={busy} onClick={() => deleteReports(deleteResults.filter((r) => !r.ok))}>
              重试失败项
            </ActionButton>
          )}
        </div>
      )}
      {confirm && (
        <AdminDialog label="确认导出">
          <div className="admin-export-confirm">
            <h2>确认生成报表</h2>
            <p>{selected.length} 家拼多多店铺，按所属节点执行</p>
            <p>
              {start.replace('T', ' ')} 至 {end.replace('T', ' ')}
            </p>
            <p>全部订单 · 无售后 / 取消售后 · 自定义报表</p>
            <div className="export-fields">
              {data?.fields.map((f) => (
                <span key={f}>{f}</span>
              ))}
            </div>
            <p>按以上 {data?.fields.length || 10} 个字段生成；已提交的任务恢复时不会重复生成。</p>
            <div className="export-actions">
              <ActionButton data-dialog-close disabled={busy} onClick={() => setConfirm(false)}>
                返回修改
              </ActionButton>
              <ActionButton className="primary" disabled={busy} onClick={submit}>
                {busy ? '提交中…' : '确认生成'}
              </ActionButton>
            </div>
          </div>
        </AdminDialog>
      )}
      <style jsx>{`
        .report-export {
          padding: 24px;
          max-width: 1800px;
          margin: auto;
        }
        .report-export header,
        .export-heading {
          display: flex;
          justify-content: space-between;
          align-items: center;
          gap: 16px;
          margin-bottom: 20px;
        }
        .report-export h1 {
          font-size: 26px;
          margin: 0;
        }
        .report-export h1 small {
          font-size: 12px;
          color: #a36414;
          background: #fff2d9;
          padding: 5px 10px;
          border-radius: 20px;
          vertical-align: middle;
        }
        .report-export p {
          color: #748299;
          font-size: 13px;
          line-height: 1.7;
        }
        .online {
          color: #159575;
        }
        .offline {
          color: #b57536;
        }
        .export-layout {
          display: grid;
          grid-template-columns: minmax(260px, 1fr) minmax(400px, 2fr);
          gap: 20px;
          margin-bottom: 20px;
        }
        .export-card {
          border: 1px solid #e4eaf3;
          background: white;
          border-radius: 16px;
          padding: 22px;
          min-width: 0;
        }
        .export-card h3 {
          margin: 0 0 14px;
        }
        .export-card h3 small {
          font-size: 13px;
          color: #8491a4;
        }
        .task-filters {
          display: flex;
          align-items: flex-end;
          gap: 12px;
          flex-wrap: wrap;
          padding: 14px;
          background: #f7f9fc;
          border-radius: 10px;
        }
        .task-filters label {
          display: flex;
          flex-direction: column;
          gap: 6px;
          font-size: 12px;
          color: #738199;
          min-width: 160px;
        }
        .selection-storage {
          display: flex;
          flex-direction: column;
          gap: 6px;
          margin-top: 14px;
          padding: 14px;
          border: 1px solid #e3eaf4;
          border-radius: 10px;
        }
        .selection-storage strong {
          font-size: 14px;
        }
        .selection-storage span,
        .selection-storage small {
          font-size: 12px;
          color: #738199;
        }
        .export-actions {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 10px;
          margin: 14px 0;
        }
        .batch-delete {
          color: #bd3d35;
        }
        .export-dates {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 14px;
        }
        .export-dates label {
          display: flex;
          flex-direction: column;
          gap: 8px;
          font-size: 13px;
        }
        .export-dates input {
          width: 100%;
          padding: 10px;
          border: 1px solid #dce4ef;
          border-radius: 8px;
          min-width: 0;
        }
        .export-fields {
          display: flex;
          flex-wrap: wrap;
          gap: 8px;
          margin: 18px 0;
        }
        .export-fields span {
          font-size: 12px;
          border-radius: 6px;
          background: #f2f6fc;
          padding: 6px 10px;
        }
        .export-card dl {
          display: grid;
          grid-template-columns: 100px 1fr;
          gap: 9px;
          font-size: 13px;
        }
        .export-card dd {
          margin: 0;
        }
        .export-card dt {
          color: #8391a5;
        }
        .export-retention {
          display: flex;
          align-items: center;
          gap: 8px;
          font-size: 13px;
        }
        .export-retention input {
          width: 55px;
          border: 1px solid #dce4ef;
          border-radius: 6px;
          padding: 6px;
        }
        .export-state {
          display: inline-block;
          border-radius: 6px;
          padding: 5px 8px;
          background: #edf4ff;
          color: #3a73bc;
          font-size: 12px;
        }
        .state-completed {
          color: #15866b;
          background: #e8f7f1;
        }
        .state-exception {
          color: #c44b3c;
          background: #fff0ec;
        }
        .report-export td small {
          display: block;
          color: #8090a5;
          font-size: 12px;
          margin-top: 5px;
        }
        .export-message {
          padding: 12px 16px;
          background: #eef5ff;
          border-radius: 10px;
        }
        @media (max-width: 850px) {
          .export-layout {
            grid-template-columns: 1fr;
          }
          .export-dates {
            grid-template-columns: 1fr;
          }
          .export-heading {
            align-items: flex-start;
            flex-direction: column;
          }
        }
      `}</style>
    </section>
  );
}
