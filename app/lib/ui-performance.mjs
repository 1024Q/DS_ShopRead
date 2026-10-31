const samples = new Map();
let longTasks = 0,
  longMs = 0;
export function recordPerformance(path, ms, cache) {
  const s = samples.get(path) || { path, times: [], hits: 0, count: 0 };
  s.times.push(Math.round(ms));
  if (s.times.length > 100) s.times.shift();
  s.count++;
  if (cache === 'hit') s.hits++;
  samples.set(path, s);
  if (samples.size > 80) samples.delete(samples.keys().next().value);
}
export function recordLongTask(ms) {
  longTasks++;
  longMs += ms;
}
export function performanceSummary() {
  return {
    longTasks,
    longMs: Math.round(longMs),
    rows: [...samples.values()].map((s) => {
      const t = [...s.times].sort((a, b) => a - b);
      return {
        path: s.path,
        count: s.count,
        last: s.times.at(-1),
        p95: t[Math.min(t.length - 1, Math.floor(t.length * 0.95))],
        hits: s.hits,
      };
    }),
  };
}
