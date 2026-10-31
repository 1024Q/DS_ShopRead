'use client';
import { useSyncExternalStore } from 'react';
import { relativeTime } from '../../lib/relative-time.mjs';
let now = 0,
  timer = null;
const listeners = new Set();
function subscribe(listener) {
  listeners.add(listener);
  now = Date.now();
  listener();
  if (!timer)
    timer = setInterval(() => {
      now = Date.now();
      listeners.forEach((fn) => fn());
    }, 15000);
  return () => {
    listeners.delete(listener);
    if (!listeners.size) {
      clearInterval(timer);
      timer = null;
    }
  };
}
export default function UpdatedAgo({ value }) {
  const clock = useSyncExternalStore(
    subscribe,
    () => now,
    () => 0,
  );
  const stamp = typeof value === 'number' ? value * 1000 : Date.parse(value);
  const title = Number.isFinite(stamp)
    ? new Date(stamp).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
    : '尚未更新';
  return (
    <span title={title} className="updated-ago">
      {clock ? relativeTime(value, clock) : title}
    </span>
  );
}
