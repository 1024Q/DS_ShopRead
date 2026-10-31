'use client';
import { useEffect, useRef, useState } from 'react';
import { sharedRead } from '../lib/shared-read.mjs';
import { overviewTarget } from '../lib/overview-navigation.mjs';
import { useWorkspace, useGlobalRefresh, useDatabaseUpdates } from '../providers/WorkspaceContext';

// Homepage count probes only inspect existing list snapshots. They must never
// enqueue a full list, even when the global button requests collection.
export default function useOverviewCounts(metric, scope, enabled = true) {
  const target = overviewTarget(metric, scope),
    { autoReady, businessDay } = useWorkspace();
  const [saved, setSaved] = useState(null),
    refresh = useRef(null);
  const query = new URLSearchParams(
    Object.entries({ ...target.filters, home_counts: 'true', page_size: '1' }).filter(
      ([, value]) => value !== '' && value != null,
    ),
  ).toString();
  const key = target.kind + ':' + query;
  useGlobalRefresh(() => refresh.current?.());
  useDatabaseUpdates(() => refresh.current?.(), { kind: target.kind, shopId: scope.shop_id, platform: scope.platform });
  useEffect(() => {
    if (!autoReady || !enabled) return;
    let active = true,
      busy = false,
      again = false;
    const controller = new AbortController();
    const load = async () => {
      if (busy) {
        again = true;
        return;
      }
      busy = true;
      try {
        const response = await sharedRead(`/api/data/${target.kind}?${query}`, {
          signal: controller.signal,
          cache: 'no-store',
        });
        if (response.ok) {
          const result = await response.json();
          if (active) setSaved({ key, result });
        }
      } catch (error) {
        if (active && error.name !== 'AbortError') setSaved(null);
      } finally {
        busy = false;
        if (active && again) {
          again = false;
          queueMicrotask(load);
        }
      }
    };
    refresh.current = load;
    load();
    return () => {
      active = false;
      controller.abort();
      refresh.current = null;
    };
  }, [autoReady, enabled, key, query, target.kind, businessDay]);
  return enabled && saved?.key === key ? saved.result : null;
}
