'use client';
import { useEffect, useRef, useState } from 'react';
import { useWorkspace, useGlobalRefresh, useDatabaseUpdates } from '../providers/WorkspaceContext';
import { sharedRead } from '../lib/shared-read.mjs';
import { throttleUpdates } from '../lib/data-updates.mjs';

// The existing overview projection reads stored snapshots only. Reuse one
// response for all store columns instead of registering extra collection jobs.
export default function useStoredOverview(shopId, platform) {
  const { businessDay, autoReady } = useWorkspace();
  const [saved, setSaved] = useState(null),
    [error, setError] = useState('');
  const refresh = useRef(null);
  useGlobalRefresh(() => refresh.current?.());
  useDatabaseUpdates(() => refresh.current?.(), {
    kinds: ['overview', 'transactions', 'ad_reports', 'orders'],
    shopId,
    platform,
  });
  useEffect(() => {
    if (!autoReady) return;
    let active = true,
      busy = false,
      queued = false;
    const controller = new AbortController();
    const load = async () => {
      if (!active || document.hidden) return;
      if (busy) {
        queued = true;
        return;
      }
      busy = true;
      queued = false;
      try {
        const response = await sharedRead('/api/home/snapshot', { signal: controller.signal, cache: 'no-store' });
        if (!response.ok) throw Error('店铺经营快照暂不可用');
        const value = await response.json();
        if (active) {
          setSaved(value);
          setError('');
        }
      } catch (e) {
        if (active && e.name !== 'AbortError') setError(e.message);
      } finally {
        busy = false;
        if (queued && active) changed();
      }
    };
    const changed = throttleUpdates(load, 3000);
    refresh.current = changed;
    changed();
    document.addEventListener('visibilitychange', changed);
    return () => {
      active = false;
      controller.abort();
      changed.cancel();
      refresh.current = null;
      document.removeEventListener('visibilitychange', changed);
    };
  }, [businessDay, autoReady]);
  return { data: saved?.day === businessDay ? saved : null, error };
}
