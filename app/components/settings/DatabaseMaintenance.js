'use client';
import { ActionButton, TextInput, Choice } from '../shared/AdminControls';
import SortableTable from '../shared/SortableTable';
import { useEffect, useState } from 'react';
import CollapsibleSection from '../shared/CollapsibleSection';
import { beijing } from '../shell/WorkspaceTools';
import { fetchJson } from '../../lib/fetch-json';
export default function DatabaseMaintenance() {
  const [backupPage, setBackupPage] = useState(1);
  const [preview, setPreview] = useState(null),
    [all, setAll] = useState(false);
  async function verify(name) {
    setBusy(true);
    setPreview(null);
    setMessage('');
    try {
      setPreview(await fetchJson('/api/system/backup-verify?' + new URLSearchParams({ name }), { method: 'POST' }));
    } catch (e) {
      setMessage(e.message);
    } finally {
      setBusy(false);
    }
  }
  const [data, setData] = useState(null),
    [draft, setDraft] = useState(null),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState('');
  async function refresh() {
    const d = await fetchJson('/api/system/maintenance');
    setData(d);
    return d;
  }
  useEffect(() => {
    let alive = true;
    fetchJson('/api/system/maintenance')
      .then((d) => {
        if (alive) {
          setData(d);
          setDraft(d.settings);
        }
      })
      .catch((e) => {
        if (alive) setMessage(e.message);
      });
    return () => {
      alive = false;
    };
  }, []);
  useEffect(() => {
    let busyRead = false;
    const t = setInterval(
      () => {
        if (document.hidden || busyRead) return;
        busyRead = true;
        refresh()
          .catch(() => {})
          .finally(() => {
            busyRead = false;
          });
      },
      data?.running || busy ? 3000 : 30000,
    );
    return () => clearInterval(t);
  }, [data?.running, busy]);
  async function action(backup) {
    setBusy(true);
    setMessage('');
    try {
      await fetchJson(backup ? '/api/system/backup' : '/api/system/maintenance', {
        method: backup ? 'POST' : 'PUT',
        headers: { 'Content-Type': 'application/json' },
        ...(backup ? {} : { body: JSON.stringify(draft) }),
      });
      const next = await refresh();
      setMessage(
        backup
          ? next?.engine === 'postgresql'
            ? '备份完成，目录和 SHA-256 校验通过；尚未执行恢复演练。'
            : '备份完成，临时恢复和完整性校验通过。'
          : next?.retention_preview?.automatic_deletion
            ? '备份设置已保存；超出保留份数的旧备份将按设置自动删除，日志与缓存保留期限仍仅预览。'
            : '备份设置已保存；保留份数与期限仅用于预览，不会删除任何文件。',
      );
    } catch (e) {
      setMessage(e.message);
    } finally {
      setBusy(false);
    }
  }
  const autoDelete = data?.retention_preview?.automatic_deletion === true;
  if (data?.engine === 'postgresql')
    return (
      <CollapsibleSection className="settings-card">
        <div className="section-head">
          <h3>中心 PostgreSQL 备份</h3>
          <ActionButton disabled={busy || data.running} onClick={() => action(true)}>
            立即备份
          </ActionButton>
        </div>
        <p>
          {autoDelete ? '中心独立备份，超出保留份数的旧备份会自动删除' : '中心独立备份，保留所有历史文件，不自动清理'}
          ；时间按 Asia/Shanghai。
        </p>
        {draft && (
          <form
            className="filter-panel"
            onSubmit={(e) => {
              e.preventDefault();
              action(false);
            }}
          >
            <label>
              每日备份
              <Choice
                value={draft.enabled ? 'on' : 'off'}
                onChange={(e) => setDraft({ ...draft, enabled: e.target.value === 'on' })}
              >
                <option value="off">暂停</option>
                <option value="on">开启</option>
              </Choice>
            </label>
            <label>
              备份时间
              <TextInput
                type="time"
                value={draft.time}
                onChange={(e) => setDraft({ ...draft, time: e.target.value })}
              />
            </label>
            <ActionButton type="submit" disabled={busy}>
              保存
            </ActionButton>
          </form>
        )}
        {message && <p role="status">{message}</p>}
        {data.error && (
          <p role="alert" className="error">
            {data.error}
          </p>
        )}
        {preview && <p>{preview.message}</p>}
        <SortableTable className="data-table">
          <thead>
            <tr>
              <th>文件</th>
              <th>大小</th>
              <th>校验</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {data.backups.map((b) => (
              <tr key={b.name}>
                <td>{b.name}</td>
                <td>{(b.size / 1048576).toFixed(1)} MB</td>
                <td>{b.verification}</td>
                <td>
                  <ActionButton disabled={busy} onClick={() => verify(b.name)}>
                    校验文件
                  </ActionButton>
                </td>
              </tr>
            ))}
            {!data.backups.length && (
              <tr>
                <td colSpan={4}>暂无备份，可点击“立即备份”创建第一份。</td>
              </tr>
            )}
          </tbody>
        </SortableTable>
      </CollapsibleSection>
    );
  return (
    <CollapsibleSection className="settings-card" aria-label="数据库备份">
      <div className="section-head">
        <div>
          <h3>数据库备份与校验</h3>
          <p>当前工作机器每天在线备份；恢复到临时数据库验证，完成后保留校验结果。</p>
        </div>
        <ActionButton disabled={busy || data?.running} onClick={() => action(true)}>
          {busy ? '处理中…' : '立即备份并校验'}
        </ActionButton>
      </div>
      {draft && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            action(false);
          }}
        >
          <fieldset disabled={busy}>
            <div className="settings-grid">
              <label>
                每日自动备份
                <Choice
                  value={draft.enabled ? 'on' : 'off'}
                  onChange={(e) => setDraft({ ...draft, enabled: e.target.value === 'on' })}
                >
                  <option value="on">开启</option>
                  <option value="off">暂停</option>
                </Choice>
              </label>
              <label>
                备份时间
                <TextInput
                  type="time"
                  required
                  value={draft.time}
                  onChange={(e) => setDraft({ ...draft, time: e.target.value })}
                />
              </label>
              {[
                ['keep', autoDelete ? '备份保留份数（超出自动删除）' : '备份保留份数（仅预览，不删除）', 2, 90],
                ['log_days', '日志保留天数（仅预览，不删除）', 7, 365],
                ['cache_days', '普通缓存保留天数（仅预览，不删除）', 7, 365],
              ].map(([key, label, min, max]) => (
                <label key={key}>
                  {label}
                  <TextInput
                    type="number"
                    min={min}
                    max={max}
                    required
                    value={draft[key]}
                    onChange={(e) => setDraft({ ...draft, [key]: Number(e.target.value) })}
                  />
                </label>
              ))}
            </div>
            <ActionButton type="submit">保存备份设置</ActionButton>
          </fieldset>
        </form>
      )}
      {!data && !message && (
        <p role="status" className="module-loading">
          正在读取备份状态…
        </p>
      )}
      {(busy || data?.running) && <p role="status">备份 / 校验执行中，状态自动更新…</p>}
      {message && <p role="status">{message}</p>}
      {data?.error && <p className="error">{data.error}</p>}
      {preview && (
        <div className="backup-preview" role="status">
          临时恢复验证通过 · 店铺 {preview.counts.shops} 家 · 报税记录 {preview.counts.tax_records} 条 · 每日归档{' '}
          {preview.counts.daily_archive} 份 · 采集范围 {preview.counts.collection_tasks} 项。当前数据库未被替换。
        </div>
      )}
      {data?.capacity && (
        <div className="backup-preview">
          <strong>{data.capacity.warning ? '磁盘空间不足预警' : '存储容量'}</strong> · 数据库合计{' '}
          {(data.capacity.database_bytes / 1073741824).toFixed(2)} GB
          {data.capacity.management_bytes != null && (
            <>
              （管理库 {(data.capacity.management_bytes / 1048576).toFixed(1)} MB ·{' '}
              {data.capacity.shop_database_count || 0} 家店铺业务库{' '}
              {(data.capacity.business_bytes / 1073741824).toFixed(2)} GB · 其中 WAL{' '}
              {(data.capacity.wal_bytes / 1048576).toFixed(1)} MB，已计入合计）
            </>
          )}{' '}
          · 备份 {(data.capacity.backup_bytes / 1073741824).toFixed(2)} GB · 磁盘剩余{' '}
          {(data.capacity.free_bytes / 1073741824).toFixed(1)} GB。仅预警，不会因磁盘空间删除数据。
        </div>
      )}
      {data && (
        <>
          <p>
            清理预览：日志 {data.retention_preview.logs} 条 · 普通缓存 {data.retention_preview.caches} 份 ·
            超出保留份数的备份 {data.retention_preview.backups} 份。
            {autoDelete
              ? '超出保留份数的旧备份、超过保留天数的日志与采集记录每天自动清理；业务缓存不自动删除。'
              : '仅为预览，当前不会自动删除任何文件。'}
            财务及报税数据不纳入普通缓存清理。
          </p>
          <p>{data.key_required}</p>
          {data.backups.length > 7 && (
            <ActionButton onClick={() => setAll(!all)}>
              {all ? '仅显示最近 7 份' : `查看全部 ${data.backups.length} 份备份`}
            </ActionButton>
          )}
          <div className="data-table-wrap">
            <SortableTable className="data-table">
              <thead>
                <tr>
                  <th>备份时间</th>
                  <th>大小</th>
                  <th>店铺数</th>
                  <th>验证结果</th>
                  <th>恢复预览</th>
                </tr>
              </thead>
              <tbody>
                {(all ? data.backups.slice((backupPage - 1) * 20, backupPage * 20) : data.backups.slice(0, 7)).map(
                  (b) => (
                    <tr key={b.name}>
                      <td title={b.name}>{beijing(b.created_at)}</td>
                      <td>{(b.size / 1048576).toFixed(1)} MB</td>
                      <td>{b.shops}</td>
                      <td>{b.verified ? '恢复验证通过' : '尚未验证'}</td>
                      <td>
                        <ActionButton disabled={busy || data?.running} onClick={() => verify(b.name)}>
                          校验并预览
                        </ActionButton>
                      </td>
                    </tr>
                  ),
                )}
              </tbody>
            </SortableTable>
            {all && (
              <div className="pagination">
                <ActionButton disabled={backupPage <= 1} onClick={() => setBackupPage((p) => p - 1)}>
                  上一页
                </ActionButton>
                <span>第 {backupPage} 页</span>
                <ActionButton
                  disabled={backupPage * 20 >= data.backups.length}
                  onClick={() => setBackupPage((p) => p + 1)}
                >
                  下一页
                </ActionButton>
              </div>
            )}
            {!data.backups.length ? (
              <p>暂无备份。</p>
            ) : (
              !data.backups.some((b) => b.verified) && (
                <p>暂无已验证备份：现有 {data.backups.length} 份备份均尚未完成恢复验证。</p>
              )
            )}
          </div>
        </>
      )}
    </CollapsibleSection>
  );
}
