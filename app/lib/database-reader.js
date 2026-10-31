import { readSnapshot, saveSnapshot, clearSnapshots, snapshotContext } from './response-snapshots.mjs';
import { sharedRead } from './shared-read.mjs';
import { canCollect } from './client-access.mjs';
// Database reads never wait for background collection registration.
const collectionFields = [
  'shop_id',
  'platform',
  'date_from',
  'date_to',
  'status',
  'stars',
  'reply',
  'period',
  'trade_range',
  'exact_dates',
];
export function dataScope(params) {
  const query = new URLSearchParams(params);
  for (const key of ['sort_by', 'sort_direction', 'page', 'page_size']) query.delete(key);
  query.sort();
  return query.toString();
}
const registrations = new Map();
export function databaseReader(path) {
  return async (params = new URLSearchParams(), { force = false, signal, onPreview, registerMissing = true } = {}) => {
    if (!canCollect(path.split('/').pop())) {
      if (force) return Response.json({ detail: '当前账号只有查看权限，不能发起采集' }, { status: 403 });
      registerMissing = false;
    }
    const query = new URLSearchParams(params);
    query.delete('refresh');
    query.delete('force');
    if (force) clearSnapshots(false);
    else {
      const previous = readSnapshot(path, query);
      if (previous && !signal?.aborted) onPreview?.(previous);
    }
    const context = snapshotContext();
    signal?.throwIfAborted();
    const reading = force ? null : sharedRead(`${path}?${query}`, { signal, cache: 'no-store' });
    const command = new URLSearchParams();
    for (const field of collectionFields) if (query.has(field)) command.set(field, query.get(field));
    command.sort();
    const key = context + ':' + path + ':' + command.toString();
    if (force) command.set('force', 'true');
    else command.set('source', 'automatic');
    const entry = registrations.get(key);
    let registration =
      !entry || Date.now() - entry.at > (force ? 1000 : 60000) || (force && !entry.force) ? null : entry.promise;
    const register = () => {
      if (!registration) {
        // A sort abort cancels its GET, not the shared, already submitted demand.
        registration = fetch(`${path}/sync?${command}`, { method: 'POST', cache: 'no-store' });
        registrations.set(key, { promise: registration, at: Date.now(), force });
        if (registrations.size > 64) registrations.delete(registrations.keys().next().value);
      }
      return registration;
    };
    // Explicit manual commands still confirm acceptance before reading their new
    // queue state. Ordinary reads never wait for a collection command.
    if (force) {
      try {
        const accepted = await register();
        if (typeof window !== 'undefined')
          window.dispatchEvent(
            new CustomEvent('collection-submitted', {
              detail: { path, scope: Object.fromEntries(command), ok: accepted.ok },
            }),
          );
        if (!accepted.ok) {
          registrations.delete(key);
          return accepted.clone();
        }
      } catch (error) {
        registrations.delete(key);
        throw error;
      }
    }
    const response = await (reading || fetch(`${path}?${query}`, { signal, cache: 'no-store' }));
    signal?.throwIfAborted();
    if (context !== snapshotContext()) throw new DOMException('查询上下文已更新', 'AbortError');
    if (!response.ok) return response;
    const data = await response.clone().json();
    if (!force && registerMissing && data.sources?.some((s) => !s.updated_at)) {
      register()
        .then((r) => {
          if (!r.ok) registrations.delete(key);
        })
        .catch(() => registrations.delete(key));
    }
    saveSnapshot(path, query, data, context);
    return response;
  };
}
