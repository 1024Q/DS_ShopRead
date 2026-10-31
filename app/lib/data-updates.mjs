import { inScope } from './filter-scope.mjs';
export const focusKinds = ['aftersales', 'evidence', 'tickets', 'intercepts', 'packages', 'invoices'];
export function updateMatches(detail, { kind, kinds, shopId, platform } = {}) {
  if (!detail || detail.reset || !detail.changes) return true;
  const needed =
    kinds ||
    (kind === 'overview'
      ? ['overview', 'transactions']
      : kind
        ? [
            kind,
            ...(['transactions', 'tax', 'reconciliation_day', 'reconciliation_month'].includes(kind)
              ? []
              : ['products', 'goodsdata']),
          ]
        : null);
  return detail.changes.some(
    (c) =>
      (!needed || c.kind === '*' || needed.includes(c.kind)) &&
      (!c.shop_id || inScope(shopId, c.shop_id)) &&
      (!c.shop_id || !c.platform || inScope(platform, c.platform)),
  );
}

// Coalesce bursts of database-updated events: run at once, then at most once
// per `wait` ms, always finishing with a trailing run so no change is dropped.
export function throttleUpdates(fn, wait = 3000, now = () => Date.now(), schedule = setTimeout, cancel = clearTimeout) {
  let last = -Infinity,
    timer = null;
  const run = () => {
    timer = null;
    last = now();
    fn();
  };
  const handler = () => {
    if (timer) return;
    const left = last + wait - now();
    if (left <= 0) run();
    else timer = schedule(run, left);
  };
  handler.cancel = () => {
    if (timer) cancel(timer);
    timer = null;
  };
  return handler;
}
