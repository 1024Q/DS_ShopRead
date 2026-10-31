'use client';
import AdminDialog from '../shared/AdminDialog';
import { ActionButton, TextInput, Choice, TextArea } from '../shared/AdminControls';
import { shopLabel } from '../../lib/shop-identity.mjs';
import { useState } from 'react';
const people = (s) => s.operators || [s.operator].filter(Boolean);
export default function OperatorManager({
  shops,
  names,
  onSave,
  onBatchSave,
  onAssign,
  onClose,
  initialSelected = [],
}) {
  const [tab, setTab] = useState(initialSelected.length ? 'assign' : 'people'),
    [preview, setPreview] = useState(false),
    [undo, setUndo] = useState(null);
  const [selected, setSelected] = useState(initialSelected),
    [persons, setPersons] = useState([]),
    [mode, setMode] = useState('append'),
    [name, setName] = useState(''),
    [batch, setBatch] = useState(''),
    [previous, setPrevious] = useState(''),
    [error, setError] = useState(''),
    [notice, setNotice] = useState(''),
    [busy, setBusy] = useState(false),
    [search, setSearch] = useState('');
  const rows = shops.filter((s) => `${shopLabel(s)} ${people(s).join(' ')}`.includes(search));
  const chosen = shops.filter((s) => selected.includes(s.id));
  const nextPeople = (s) =>
    mode === 'clear'
      ? []
      : mode === 'replace'
        ? persons
        : mode === 'remove'
          ? people(s).filter((n) => !persons.includes(n))
          : [...new Set([...people(s), ...persons])];
  const batchNames = [
    ...new Set(
      batch
        .split(/[,，、;；\n\r]+/)
        .map((n) => n.trim())
        .filter(Boolean),
    ),
  ];
  async function perform(fn) {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await fn();
      window.dispatchEvent(new Event('dialog-saved'));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <AdminDialog>
      <section className="admin-dialog-content operator-dialog" aria-label="运营人员管理">
        <div className="dialog-head">
          <h2>运营人员管理</h2>
          <ActionButton disabled={busy} onClick={onClose}>
            关闭
          </ActionButton>
        </div>
        <div className="ov-segments">
          <ActionButton aria-pressed={tab === 'people'} onClick={() => setTab('people')}>
            人员管理
          </ActionButton>
          <ActionButton aria-pressed={tab === 'assign'} onClick={() => setTab('assign')}>
            店铺分配
          </ActionButton>
        </div>
        {tab === 'people' && (
          <>
            <form
              className="operator-add"
              onSubmit={(e) => {
                e.preventDefault();
                perform(async () => {
                  await onSave(name, previous);
                  setPersons([]);
                  setName('');
                  setPrevious('');
                  setNotice('人员已保存');
                });
              }}
            >
              <TextInput
                aria-label="运营人员姓名"
                placeholder="输入运营人员姓名"
                required
                maxLength={100}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
              <ActionButton type="submit" className="primary" disabled={busy}>
                {previous ? '保存改名' : '添加人员'}
              </ActionButton>
              {previous && (
                <ActionButton
                  type="button"
                  onClick={() => {
                    setPrevious('');
                    setName('');
                  }}
                >
                  取消改名
                </ActionButton>
              )}
            </form>
            <details className="operator-batch-add">
              <summary>批量添加运营人员</summary>
              <TextArea
                aria-label="批量运营人员姓名"
                placeholder="每行一位，也可用逗号分隔"
                maxLength={10000}
                value={batch}
                onChange={(e) => setBatch(e.target.value)}
              />
              <ActionButton
                disabled={busy || !batchNames.length || batchNames.length > 100}
                onClick={() =>
                  perform(async () => {
                    await onBatchSave(batchNames);
                    setBatch('');
                    setNotice(`已添加 ${batchNames.length} 位运营人员（重名自动跳过）`);
                  })
                }
              >
                添加 {batchNames.length} 位人员
              </ActionButton>
            </details>
            <div className="operator-tags">
              {names.map((n) => (
                <ActionButton
                  disabled={busy}
                  key={n}
                  title="点击改名，已分配店铺同步更新"
                  onClick={() => {
                    setPrevious(n);
                    setName(n);
                  }}
                >
                  {n} · {shops.filter((s) => people(s).includes(n)).length} 家 <small>编辑</small>
                </ActionButton>
              ))}
            </div>
          </>
        )}
        {tab === 'assign' && (
          <>
            <h3>批量分配店铺</h3>
            <div className="operator-person-choices" role="group" aria-label="选择运营人员">
              {names.map((n) => (
                <label key={n}>
                  <TextInput
                    disabled={busy}
                    type="checkbox"
                    checked={persons.includes(n)}
                    onChange={(e) => setPersons((old) => (e.target.checked ? [...old, n] : old.filter((p) => p !== n)))}
                  />
                  {n}
                </label>
              ))}
              {!names.length && <span>请先添加运营人员</span>}
            </div>
            <div className="operator-bulk">
              <TextInput
                aria-label="搜索待分配店铺"
                placeholder="搜索店铺或负责人"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <Choice aria-label="批量分配方式" value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="append">追加运营</option>
                <option value="replace">替换全部运营</option>
                <option value="remove">移除所选运营</option>
                <option value="clear">清空运营</option>
              </Choice>
              <ActionButton
                className="primary"
                disabled={busy || !selected.length || (mode !== 'clear' && !persons.length)}
                onClick={() => setPreview(true)}
              >
                预览 {selected.length} 家店铺的修改
              </ActionButton>
            </div>
            <p className="data-note">
              已选 {selected.length} 家，其中 {selected.filter((id) => !rows.some((s) => s.id === id)).length}{' '}
              家不在当前筛选结果中。
            </p>
            <label className="operator-select-all">
              <TextInput
                disabled={busy}
                type="checkbox"
                checked={!!rows.length && rows.every((s) => selected.includes(s.id))}
                onChange={(e) =>
                  setSelected((old) =>
                    e.target.checked
                      ? [...new Set([...old, ...rows.map((s) => s.id)])]
                      : old.filter((id) => !rows.some((s) => s.id === id)),
                  )
                }
              />
              全选当前结果
            </label>
            <div className="operator-shop-list">
              {rows.map((s) => (
                <label key={s.id}>
                  <TextInput
                    disabled={busy}
                    type="checkbox"
                    checked={selected.includes(s.id)}
                    onChange={(e) =>
                      setSelected((old) => (e.target.checked ? [...old, s.id] : old.filter((id) => id !== s.id)))
                    }
                  />
                  <span>{shopLabel(s)}</span>
                  <small>{people(s).join('、') || '未分配'}</small>
                </label>
              ))}
            </div>
          </>
        )}
        {preview && (
          <div className="operator-preview">
            <strong>分配修改预览</strong>
            {chosen.map((s) => (
              <p key={s.id}>
                {shopLabel(s)}：{people(s).join('、') || '未分配'} → {nextPeople(s).join('、') || '未分配'}
              </p>
            ))}
            <ActionButton disabled={busy} onClick={() => setPreview(false)}>
              取消
            </ActionButton>
            <ActionButton
              disabled={busy}
              className="primary"
              onClick={() =>
                perform(async () => {
                  const previous = chosen.map((s) => ({ id: s.id, names: people(s) }));
                  await onAssign(selected, mode === 'clear' ? [] : persons, mode === 'clear' ? 'replace' : mode);
                  setUndo(previous);
                  setPreview(false);
                  setNotice(`已更新 ${selected.length} 家店铺`);
                  setSelected([]);
                })
              }
            >
              确认分配
            </ActionButton>
          </div>
        )}
        {undo && (
          <ActionButton
            disabled={busy}
            onClick={() =>
              perform(async () => {
                for (const s of undo) await onAssign([s.id], s.names, 'replace');
                setUndo(null);
                setNotice('已撤回本次分配');
              })
            }
          >
            撤回本次分配
          </ActionButton>
        )}
        {notice && <p role="status">{notice}</p>}
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
      </section>
    </AdminDialog>
  );
}
