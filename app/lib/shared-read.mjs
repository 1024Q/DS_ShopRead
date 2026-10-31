import { snapshotContext } from './response-snapshots.mjs';
import { recordPerformance } from './ui-performance.mjs';
const requests = new Map();
export function parsedResponse(response, data) {
  return {
    ok: response.ok,
    status: response.status,
    headers: response.headers,
    json: async () => data,
    clone() {
      return this;
    },
  };
}
export async function sharedRead(url, { signal, ...options } = {}) {
  signal?.throwIfAborted();
  const normalized = new URL(url, 'http://local');
  normalized.searchParams.sort();
  const key = snapshotContext() + ':' + normalized.pathname + normalized.search;
  let entry = requests.get(key);
  if (!entry) {
    const start = performance.now();
    entry = {
      promise: fetch(url, { ...options, signal: AbortSignal.timeout(30000) }).then(async (r) => {
        const data = await r.json();
        recordPerformance(normalized.pathname, performance.now() - start, r.headers.get('X-Query-Cache'));
        return parsedResponse(r, data);
      }),
    };
    requests.set(key, entry);
    const clear = () => {
      if (requests.get(key) === entry) requests.delete(key);
    };
    entry.promise.then(clear, clear);
    if (requests.size > 100) requests.delete(requests.keys().next().value);
  }
  const response = await entry.promise;
  signal?.throwIfAborted();
  return response;
}
