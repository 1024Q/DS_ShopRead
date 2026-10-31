const offset = 8 * 3600000,
  day = 86400000;
export const beijingDay = (now = Date.now()) => new Date(now + offset).toISOString().slice(0, 10);
export const nextBeijingMidnight = (now = Date.now()) => (Math.floor((now + offset) / day) + 1) * day - offset;
export function watchBeijingDay(
  onChange,
  {
    now = Date.now,
    schedule = setTimeout,
    cancel = clearTimeout,
    document: doc = globalThis.document,
    window: win = globalThis.window,
  } = {},
) {
  let previous = beijingDay(now()),
    timer,
    stopped = false;
  const check = () => {
    if (stopped) return;
    cancel(timer);
    const stamp = now(),
      current = beijingDay(stamp);
    if (current !== previous) {
      previous = current;
      onChange(current);
    }
    timer = schedule(check, Math.min(30000, Math.max(25, nextBeijingMidnight(stamp) - stamp + 25)));
  };
  doc?.addEventListener('visibilitychange', check);
  win?.addEventListener('focus', check);
  check();
  return () => {
    stopped = true;
    cancel(timer);
    doc?.removeEventListener('visibilitychange', check);
    win?.removeEventListener('focus', check);
  };
}
