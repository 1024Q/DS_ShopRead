'use client';
import { databaseReader, dataScope } from '../lib/database-reader';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useWorkspace, useGlobalRefresh, useDatabaseUpdates } from '../providers/WorkspaceContext';
export default function useReportData(kind, values, enabled = true) {
  const { auto, autoReady, live, businessDay, pauseVersion } = useWorkspace(),
    [saved, setSaved] = useState(null),
    [error, setError] = useState(''),
    [pending, setPending] = useState(false);
  const lastBusinessDay = useRef(businessDay);
  const ref = useRef(null),
    query = new URLSearchParams(
      Object.entries({ ...values, ...(values.dashboard ? { business_day: businessDay } : {}) }).filter(
        ([, v]) => v !== '' && v != null,
      ),
    ).toString();
  const scope = kind + ':' + dataScope(query),
    read = useMemo(() => databaseReader(`/api/data/${kind}`), [kind]);
  useGlobalRefresh((mode) => ref.current?.(mode === 'collect', mode === 'collect'));
  useDatabaseUpdates(() => ref.current?.(false), { kind, shopId: values.shop_id, platform: values.platform });
  useEffect(() => {
    if (!autoReady || !enabled) return;
    let alive = true,
      busy = false,
      queuedForce = false,
      queuedRead = false,
      current = null,
      last = 0;
    const c = new AbortController();
    setError('');
    async function load(force = false, registerMissing = true) {
      if (busy || document.hidden) {
        queuedForce ||= force;
        queuedRead ||= !force;
        return;
      }
      force ||= queuedForce;
      queuedForce = false;
      queuedRead = false;
      const generation = pauseVersion.current;
      busy = true;
      setPending(true);
      try {
        const params = new URLSearchParams(query);
        const response = await read(params, {
          force,
          registerMissing,
          signal: c.signal,
          onPreview: (result) => {
            if (alive && generation === pauseVersion.current && !current) setSaved({ scope, query, result });
          },
        });
        const result = await response.json();
        if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : '数据读取失败');
        if (alive && generation === pauseVersion.current) {
          current = result;
          setSaved({ scope, query, result });
          setError('');
        }
        last = Date.now();
      } catch (e) {
        if (alive && e.name !== 'AbortError') setError(e.message);
      } finally {
        last = Date.now();
        busy = false;
        if (alive) {
          setPending(false);
          if ((queuedForce || queuedRead) && !document.hidden) queueMicrotask(() => load(queuedForce));
        }
      }
    }
    const crossedDay = !!values.dashboard && lastBusinessDay.current !== businessDay;
    lastBusinessDay.current = businessDay;
    ref.current = load;
    load();

    return () => {
      alive = false;
      c.abort();
      ref.current = null;
    };
  }, [read, scope, query, autoReady, enabled, businessDay]);
  return {
    data: enabled && saved?.scope === scope ? saved.result : null,
    error: enabled ? error : '',
    loading: enabled && (pending || (!error && saved?.query !== query)),
    refresh: (force = true) => ref.current?.(force),
  };
}
