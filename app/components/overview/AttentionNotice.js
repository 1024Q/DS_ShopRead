'use client';
import { ActionButton } from '../shared/AdminControls';
import { createContext, useContext, useEffect, useRef, useState } from 'react';
const NoticeContext = createContext(null);
export default function AttentionNotice({ connected, children }) {
  const [count, setCount] = useState(0),
    [error, setError] = useState('');
  const audio = useRef(null),
    storage = useRef('');
  // Keep the notifier mounted across navigation; unlock audio on a real user gesture.
  useEffect(() => {
    let alive = true,
      busy = false;
    async function activate() {
      if (busy || audio.current?.state === 'running') return;
      const Audio = window.AudioContext || window.webkitAudioContext;
      if (!Audio) return;
      busy = true;
      try {
        audio.current ??= new Audio();
        await audio.current.resume();
      } catch {
      } finally {
        busy = false;
      }
    }
    const interact = () => {
      if (alive) activate();
    };
    document.addEventListener('pointerdown', interact);
    document.addEventListener('keydown', interact);
    return () => {
      alive = false;
      document.removeEventListener('pointerdown', interact);
      document.removeEventListener('keydown', interact);
      const ctx = audio.current;
      audio.current = null;
      if (ctx) ctx.close().catch(() => {});
    };
  }, []);
  useEffect(() => {
    if (!connected) return;
    let alive = true,
      busy = false,
      machine = '',
      cursor = null;
    const abort = new AbortController();
    async function poll() {
      if (busy || document.hidden) return;
      busy = true;
      try {
        const r = await fetch('/api/attention/changes' + (cursor === null ? '' : `?after=${cursor}`), {
          cache: 'no-store',
          signal: abort.signal,
        });
        if (!r.ok) throw Error();
        const d = await r.json();
        if (!alive) return;
        if (machine !== d.machine) {
          machine = d.machine;
          storage.current = `commerce:notice:${machine}`;
          let saved = {};
          try {
            saved = JSON.parse(localStorage.getItem(storage.current) || '{}');
          } catch {}
          cursor = Math.min(saved.cursor ?? d.latest, d.latest);
          setCount(saved.count || 0);
          setError('');
          return;
        }
        let saved = {};
        try {
          saved = JSON.parse(localStorage.getItem(storage.current) || '{}');
        } catch {}
        const fresh = d.rows.filter((x) => x.id > Math.max(cursor, saved.cursor || 0));
        // Coalesce repeated events for one order within this delivery batch.
        const changes = new Set(fresh.map((x) => `${x.shop_id}:${x.kind}:${x.record_id}`));
        const next = (saved.count || 0) + changes.size;
        cursor = d.cursor;
        setCount(next);
        setError('');
        try {
          localStorage.setItem(storage.current, JSON.stringify({ cursor, count: next }));
        } catch {}
        if (changes.size && audio.current?.state === 'running') {
          const tone = audio.current.createOscillator(),
            gain = audio.current.createGain();
          tone.connect(gain);
          gain.connect(audio.current.destination);
          gain.gain.value = 0.06;
          tone.frequency.value = 660;
          tone.start();
          tone.stop(audio.current.currentTime + 0.16);
        }
      } catch (e) {
        if (alive && e.name !== 'AbortError') setError('提醒连接暂不可用');
      } finally {
        busy = false;
      }
    }
    poll();
    const timer = setInterval(poll, 60000);
    const updatedData = (e) => {
      if (
        e.detail?.reset ||
        e.detail?.changes?.some((c) =>
          ['aftersales', 'evidence', 'tickets', 'intercepts', 'packages', 'invoices'].includes(c.kind),
        )
      )
        poll();
    };
    window.addEventListener('database-updated', updatedData);
    document.addEventListener('visibilitychange', poll);
    const updated = (e) => {
      if (e.key === storage.current) {
        try {
          setCount(JSON.parse(e.newValue || '{}').count || 0);
        } catch {}
      }
    };
    window.addEventListener('storage', updated);
    return () => {
      alive = false;
      abort.abort();
      clearInterval(timer);
      window.removeEventListener('database-updated', updatedData);
      document.removeEventListener('visibilitychange', poll);
      window.removeEventListener('storage', updated);
    };
  }, [connected]);
  function acknowledge() {
    try {
      const value = JSON.parse(localStorage.getItem(storage.current) || '{}');
      localStorage.setItem(storage.current, JSON.stringify({ ...value, count: 0 }));
    } catch {}
    setCount(0);
  }
  return <NoticeContext.Provider value={{ count, error, connected, acknowledge }}>{children}</NoticeContext.Provider>;
}
export function AttentionNoticeEntry({ onNavigate }) {
  const notice = useContext(NoticeContext);
  if (!notice) return null;
  const { count, error, connected, acknowledge } = notice;
  return (
    <ActionButton
      className={`ov-change-notice${count ? ' has-changes' : ''}`}
      onClick={() => {
        acknowledge();
        onNavigate('todos', { tab: 'changes' });
      }}
    >
      <span className="ov-change-icon" aria-hidden="true">
        ↻
      </span>
      <span className="ov-change-copy">
        <strong>
          重点变化 <b aria-live="polite">{count > 99 ? '99+' : count}</b>
        </strong>
        <small>
          {!connected
            ? '服务未连接'
            : error
              ? '提醒连接中断，正在重连'
              : count
                ? '有新的事项变化，点击查看'
                : '暂无未读变化 · 查看变化记录'}
        </small>
      </span>
      <span className="ov-change-link">查看记录 ↗</span>
    </ActionButton>
  );
}
