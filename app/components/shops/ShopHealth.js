'use client';
import { useEffect, useState } from 'react';
export function useShopHealth(enabled) {
  const [data, setData] = useState(null),
    [error, setError] = useState('');
  useEffect(() => {
    if (!enabled) return;
    let active = true,
      busy = false;
    const c = new AbortController();
    async function load() {
      if (busy || document.hidden) return;
      busy = true;
      try {
        const r = await fetch('/api/system/health', { cache: 'no-store', signal: c.signal });
        if (!r.ok) throw Error('健康状态读取失败');
        const d = await r.json();
        if (active) {
          setData(d);
          setError('');
        }
      } catch (e) {
        if (active && e.name !== 'AbortError') setError(e.message);
      } finally {
        busy = false;
      }
    }
    load();
    const t = setInterval(load, 15000);
    return () => {
      active = false;
      c.abort();
      clearInterval(t);
    };
  }, [enabled]);
  return { data, error };
}
