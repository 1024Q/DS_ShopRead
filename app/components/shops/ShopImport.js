'use client';
import AdminDialog from '../shared/AdminDialog';
import { ActionButton, TextInput, Choice, TextArea } from '../shared/AdminControls';
import ShopNodeSelect from './ShopNodeSelect';
import SortableTable from '../shared/SortableTable';
import { useRef, useState } from 'react';
import { platforms } from '../../lib/platforms.mjs';
const MAX = 1024 * 1024;
export default function ShopImport({ onClose, onImported, defaultPlatform = 'pdd' }) {
  const [content, setContent] = useState(''),
    [browser, setBrowser] = useState('system'),
    [result, setResult] = useState(null),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false),
    [filename, setFilename] = useState('');
  const [node, setNode] = useState(''),
    [center, setCenter] = useState(null);
  const [platform, setPlatform] = useState(defaultPlatform);
  const input = useRef(null);
  const change = (value) => {
    setContent(value);
    setResult(null);
    setError('');
  };
  async function fileSelected(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    setResult(null);
    setError('');
    setContent('');
    setFilename('');
    if (file.size > MAX) {
      setError('文件不能超过 1 MB');
      e.target.value = '';
      return;
    }
    setBusy(true);
    try {
      const bytes = await file.arrayBuffer();
      let text;
      try {
        text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
      } catch {
        text = new TextDecoder('gb18030', { fatal: true }).decode(bytes);
      }
      setContent(text);
      setFilename(file.name);
    } catch {
      setError('文件读取失败，请使用 UTF-8 或 GB18030 编码的 CSV / TXT 文件');
    } finally {
      setBusy(false);
      e.target.value = '';
    }
  }
  function template() {
    const blob = new Blob(['\ufeff账号,密码,店铺名称,浏览器,平台\r\n'], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = '店铺批量导入模板.csv';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function submit(commit) {
    if (busy) return;
    if (center === null) {
      setError('正在读取采集节点，请稍后');
      return;
    }
    if (center && !node) {
      setError('请选择所属采集节点');
      return;
    }
    if (new TextEncoder().encode(content).length > MAX) {
      setError('导入内容不能超过 1 MB');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const response = await fetch(node ? `/api/nodes/${node}/proxy/shops/import` : '/api/shops/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, browser, platform, commit }),
      });
      const d = await response.json();
      if (!response.ok) throw Error(typeof d.detail === 'string' ? d.detail : '导入内容无效，请检查格式');
      setResult(d);
      if (d.committed) {
        setContent('');
        setFilename('');
        await onImported(d, node);
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  const done = result?.committed;
  return (
    <AdminDialog>
      <section className="admin-dialog-content import-dialog" aria-label="批量导入店铺">
        <div className="dialog-head">
          <div>
            <h2>批量导入店铺</h2>
            <p>一次导入，集中管理多个店铺。</p>
          </div>
          <ActionButton disabled={busy} onClick={onClose} aria-label="关闭批量导入">
            ×
          </ActionButton>
        </div>
        {!done && (
          <>
            <ShopNodeSelect
              value={node}
              onChange={(v) => {
                setNode(v);
                setResult(null);
              }}
              onMode={setCenter}
              disabled={busy}
            />{' '}
            <div className="import-guide">
              <strong>列顺序：账号、密码、店铺名称、浏览器、平台</strong>
              <p>
                支持带表头的 CSV／TXT，或从 Excel 复制后粘贴；名称和浏览器可留空。浏览器填写 system（系统）或
                bundled（项目），最多 500 行。同平台已有账号自动跳过；平台可填写中文名称，留空使用默认平台。
              </p>
            </div>
            <div className="import-tools">
              <TextInput ref={input} type="file" accept=".csv,.txt,.tsv" hidden onChange={fileSelected} />
              <ActionButton disabled={busy} onClick={() => input.current?.click()}>
                选择 CSV / TXT 文件
              </ActionButton>
              <ActionButton disabled={busy} onClick={template}>
                下载空白模板
              </ActionButton>
              {filename && <span>{filename}</span>}
              <label>
                默认平台
                <Choice
                  aria-label="导入默认平台"
                  disabled={busy}
                  value={platform}
                  onChange={(e) => {
                    setPlatform(e.target.value);
                    setResult(null);
                  }}
                >
                  {Object.entries(platforms).map(([k, v]) => (
                    <option key={k} value={k}>
                      {v}
                    </option>
                  ))}
                </Choice>
              </label>
              <label>
                默认浏览器
                <Choice
                  aria-label="导入默认浏览器"
                  disabled={busy}
                  value={browser}
                  onChange={(e) => {
                    setBrowser(e.target.value);
                    setResult(null);
                  }}
                >
                  <option value="system">系统 Google Chrome</option>
                  <option value="bundled">项目自带 Google Chrome</option>
                </Choice>
              </label>
            </div>
            <label className="import-content-label">
              导入内容
              <TextArea
                aria-label="店铺导入内容"
                spellCheck={false}
                autoComplete="off"
                disabled={busy}
                value={content}
                maxLength={MAX}
                onChange={(e) => change(e.target.value)}
                placeholder={'账号\t密码\t店铺名称\t浏览器\n从 Excel 复制后粘贴，或选择文件'}
              />
            </label>
            <p className="form-hint">
              下方预览表不显示密码；确认导入后，账号配置与加密密码保存到当前服务的数据库。导入内容不会保存在浏览器中。
            </p>
          </>
        )}
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        {result && (
          <>
            <div className={`import-result ${result.invalid ? 'has-errors' : ''}`} role="status">
              {done
                ? `已导入 ${result.imported} 家店铺，跳过 ${result.skipped} 家已有 / 重复账号。`
                : `共 ${result.total} 行，可导入 ${result.ready} 家，跳过 ${result.skipped} 家，错误 ${result.invalid} 行。`}
              {result.invalid > 0 && <p>请修正错误行后重新预览，本批次尚未保存。</p>}
            </div>
            <div className="data-table-wrap import-preview">
              <SortableTable className="data-table">
                <thead>
                  <tr>
                    {['行号', '账号', '平台', '店铺名称', '浏览器', '结果'].map((t) => (
                      <th key={t}>{t}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {result.rows.map((r) => (
                    <tr key={r.line} className={r.status === 'invalid' ? 'import-invalid' : ''}>
                      <td>{r.line}</td>
                      <td>{r.username || '未填写'}</td>
                      <td>{platforms[r.platform] || '—'}</td>
                      <td>{r.name || '登录后识别'}</td>
                      <td>
                        {r.browser === 'system' ? '系统 Chrome' : r.browser === 'bundled' ? '项目 Chrome' : '无效'}
                      </td>
                      <td>{r.message}</td>
                    </tr>
                  ))}
                </tbody>
              </SortableTable>
            </div>
          </>
        )}
        <div className="import-footer">
          <ActionButton disabled={busy} onClick={onClose}>
            {done ? '完成' : '取消'}
          </ActionButton>
          {!done && (
            <>
              <ActionButton disabled={busy || center === null || !content.trim()} onClick={() => submit(false)}>
                {busy ? '处理中…' : '预览并校验'}
              </ActionButton>
              <ActionButton
                className="primary"
                disabled={busy || !result || !!result.invalid || !result.ready}
                onClick={() => submit(true)}
              >
                确认导入 {result?.ready || 0} 家
              </ActionButton>
            </>
          )}
        </div>
      </section>
    </AdminDialog>
  );
}
