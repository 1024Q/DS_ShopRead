// Same-day, per-tab previews. No credentials; scoped by authenticated service
// and active machine, plus the complete query (including sort and pagination).
const prefix = 'pdd-preview-v1:',
  ttl = 24 * 60 * 60 * 1000,
  maxEntries = 64,
  maxBytes = 4 * 1024 * 1024;
import { setPreferenceScope } from './preference-scope.mjs';
let scope = '',
  generation = 0,
  memory = new Map(),
  disk = null,
  pending = new Map(),
  scheduled = false;
export const snapshotContext = () => scope + ':' + generation;
export function setSnapshotScope(value) {
  scope = value || '';
  generation++;
  setPreferenceScope(scope);
}
export function clearSnapshots(fence = true) {
  if (fence) generation++;
  memory.clear();
  pending.clear();
  disk = null;
  try {
    for (const key of Object.keys(sessionStorage)) if (key.startsWith(prefix)) sessionStorage.removeItem(key);
  } catch {}
}
function keyFor(path, params) {
  const p = new URLSearchParams(params);
  p.sort();
  return prefix + scope + ':' + new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10) + ':' + path + '?' + p;
}
export function readSnapshot(path, params) {
  if (!scope) return null;
  const key = keyFor(path, params);
  try {
    const item = memory.get(key) || JSON.parse(sessionStorage.getItem(key) || 'null');
    if (!item || Date.now() - item.at > ttl) {
      memory.delete(key);
      sessionStorage.removeItem(key);
      return null;
    }
    return {
      ...item.result,
      _preview_cached_at: item.at,
      sources: item.result.sources?.map((s) => ({ ...s, _preview_cached_at: item.at })),
    };
  } catch {
    return null;
  }
}
export function saveSnapshot(path, params, result, context = snapshotContext()) {
  if (!scope || context !== snapshotContext()) return;
  const key = keyFor(path, params),
    item = { at: Date.now(), result };
  try {
    item.bytes = memory.get(key)?.bytes || 65536;
    memory.delete(key);
    memory.set(key, item);
    let memoryBytes = [...memory.values()].reduce((n, i) => n + (i.bytes || 0), 0);
    while (memory.size > maxEntries || memoryBytes > maxBytes) {
      const key = memory.keys().next().value;
      memoryBytes -= memory.get(key).bytes || 0;
      memory.delete(key);
    }
    pending.set(key, item);
    if (typeof window === 'undefined') flush();
    else if (!scheduled) {
      scheduled = true;
      (window.requestIdleCallback || ((fn) => setTimeout(fn, 50)))(flush);
    }
  } catch {
    /* Storage unavailable/full: fresh database reads still work. */
  }
}

function flush() {
  scheduled = false;
  try {
    if (!disk)
      disk = new Map(
        Object.keys(sessionStorage)
          .filter((k) => k.startsWith(prefix))
          .map((k) => [k, (sessionStorage.getItem(k) || '').length * 2]),
      );
    let size = [...disk.values()].reduce((a, b) => a + b, 0);
    for (const [key, item] of pending) {
      const text = JSON.stringify(item);
      if (text.length * 2 > maxBytes) {
        memory.delete(key);
        continue;
      }
      item.bytes = text.length * 2;
      size -= disk.get(key) || 0;
      disk.delete(key);
      while (disk.size >= maxEntries || size + text.length * 2 > maxBytes) {
        const old = disk.keys().next().value;
        if (!old) break;
        size -= disk.get(old);
        disk.delete(old);
        sessionStorage.removeItem(old);
      }
      sessionStorage.setItem(key, text);
      disk.set(key, text.length * 2);
      size += text.length * 2;
    }
  } catch {
    disk = null;
  } finally {
    pending.clear();
  }
}
