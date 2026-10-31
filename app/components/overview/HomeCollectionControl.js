'use client';
import { ActionButton, Choice } from '../shared/AdminControls';
import { Popover } from 'antd';
import { useEffect, useRef, useState } from 'react';

export default function HomeCollectionControl({ compact = false }) {
  const sequence = useRef(0),
    saving = useRef(false);
  const [data, setData] = useState(null),
    [scope, setScope] = useState('0'),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState(''),
    [choices, setChoices] = useState([]);
  async function request(method = 'GET', paused) {
    const r = await fetch('/api/system/home-collection', {
      method,
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'x-ecmp-config-node': scope },
      body: method === 'PUT' ? JSON.stringify({ paused }) : undefined,
      signal: AbortSignal.timeout(30000),
    });
    const value = await r.json();
    if (!r.ok) throw Error(value.detail || '采集控制暂不可用');
    return value.nodes
      ? value
      : {
          ...value,
          nodes: [{ id: 0, name: '当前采集节点', paused: value.paused, confirmed: value.confirmed, online: true }],
        };
  }
  useEffect(() => {
    let alive = true;
    const load = async () => {
      if (saving.current) return;
      const version = sequence.current;
      try {
        const value = await request();
        if (alive && version === sequence.current) {
          setData(value);
          setMessage('');
          if (scope === '0') setChoices(value.nodes);
        }
      } catch (e) {
        if (alive && version === sequence.current && !saving.current) setMessage(e.message);
      }
    };
    load();
    const timer = setInterval(() => {
      if (!document.hidden) load();
    }, 10000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [scope]);
  async function change(paused) {
    saving.current = true;
    sequence.current++;
    setBusy(true);
    setMessage(paused ? '正在停止派发、取消读取并保存断点…' : '正在恢复首页采集…');
    try {
      const value = await request('PUT', paused);
      setData(value);
      setMessage(
        value.pending
          ? `指令已保存，${value.pending} 台节点待确认，连接后自动下发。`
          : paused
            ? '首页采集已暂停，数据和断点已保留。'
            : '首页采集已恢复，休眠店铺按名额轮流唤醒。',
      );
    } catch (e) {
      setMessage(e.message + '；请读取下方节点状态确认结果。');
    } finally {
      saving.current = false;
      setBusy(false);
    }
  }
  const nodes = data?.nodes || [];
  const controls = (
    <section className="home-collection-control" aria-label="首页采集控制">
      <div className="controls">
        <strong>首页采集</strong>
        <Choice
          aria-label="首页采集机器范围"
          disabled={busy}
          value={scope}
          onChange={(e) => {
            setScope(e.target.value);
            setData(null);
            setMessage('');
          }}
        >
          <option value="0">全部采集机器</option>
          {choices
            .filter((n) => n.id !== 0)
            .map((n) => (
              <option key={n.id} value={String(n.id)}>
                {n.name}
              </option>
            ))}
        </Choice>
        <ActionButton disabled={busy || !data || !nodes.length || data.paused === true} onClick={() => change(true)}>
          暂停首页采集
        </ActionButton>
        <ActionButton
          className="primary"
          disabled={busy || !data || !nodes.length || data.paused === false}
          onClick={() => change(false)}
        >
          恢复首页采集
        </ActionButton>
        <small>仅控制首页任务；手动报表导出独立执行。</small>
      </div>
      {message && <p role="status">{message}</p>}
      <div className="nodes">
        {nodes.map((n) => (
          <span key={n.id}>
            {n.name} · {n.confirmed ? (n.paused ? '已暂停' : '采集中') : !n.online ? '离线 · 待下发' : '等待节点确认'}
          </span>
        ))}
      </div>
      <style jsx>{`
        .home-collection-control {
          box-sizing: border-box;
          width: 680px;
          max-width: min(680px, calc(100vw - 48px));
          max-height: 70vh;
          overflow: auto;
          padding: 16px 18px;
          border: 1px solid var(--shell-border, #e3e9f1);
          background: var(--panel-bg, #fff);
          border-radius: 14px;
          margin-bottom: 18px;
        }
        .controls {
          display: flex;
          align-items: center;
          gap: 10px;
          flex-wrap: wrap;
        }
        .controls small {
          color: var(--shell-muted, #748299);
        }
        .nodes {
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
          margin-top: 10px;
        }
        .nodes span {
          font-size: 12px;
          padding: 5px 9px;
          border-radius: 8px;
          background: var(--soft-bg, #f2f5fa);
          color: var(--shell-muted, #53647c);
        }
        .home-collection-control p {
          font-size: 13px;
          color: var(--shell-muted, #53647c);
          margin: 10px 0 0;
        }
      `}</style>
    </section>
  );
  return compact ? (
    <Popover content={controls} trigger="click" placement="bottomRight">
      <ActionButton className="home-collection-trigger" aria-label="首页采集设置">
        首页采集 ·{' '}
        {busy
          ? '处理中'
          : !data
            ? '读取中'
            : data.paused === true
              ? '已暂停'
              : data.paused === false
                ? '采集中'
                : '部分暂停'}{' '}
        ▾
      </ActionButton>
    </Popover>
  ) : (
    controls
  );
}
