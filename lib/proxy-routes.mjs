// Each entry declares both the allowed route and its destination scope.
export const routes = [
  { pattern: /^home\/snapshot$/, scope: 'center' },
  { pattern: /^shops\/\d+\/refund-actions$/, scope: 'node' },
  { pattern: /^nodes(?:\/\d+)?$/, scope: 'center' },
  { pattern: /^data\/(orders|aftersales|tickets)\/complete-once$/, scope: 'center' },
  { pattern: /^(?:table-tools\/(?:orders|cost\/[a-f0-9]{32}|exports(?:\/[a-f0-9]{32}\/download)?))$/, scope: 'center' },
  { pattern: /^(?:cost-supplements(?:\/(?:template|manual-sku|[a-f0-9]{32}\/retry))?)$/, scope: 'center' },
  { pattern: /^(?:profit(?:\/\d+\/20\d{2}-\d{2}(?:\/confirm)?)?)$/, scope: 'center' },
  { pattern: /^(?:profit-tax(?:\/\d+\/20\d{2}-\d{2})?)$/, scope: 'center' },
  { pattern: /^(?:promotion-ledger)$/, scope: 'center' },
  { pattern: /^(?:cost-tables(?:\/(?:preview|[a-f0-9]{32}(?:\/activate)?))?)$/, scope: 'center' },
  {
    pattern:
      /^(?:report-exports(?:\/(?:settings|view|unmatched|[a-f0-9]{32}(?:\/(?:retry|download|import|screenshot|recalculate))?))?)$/,
    scope: 'center',
  },
  {
    pattern:
      /^(?:invoice-imports(?:\/[a-f0-9]{32}(?:\/(?:confirm|files(?:\/[a-f0-9]{32}\/(?:match|pdf|cancel))?))?)?)$/,
    scope: 'node',
  },
  { pattern: /^(?:invoices\/refresh-pending)$/, scope: 'node' },
  { pattern: /^(?:exports\/[a-f0-9]{32}(?:\/download)?)$/, scope: 'node' },
  { pattern: /^(?:fleet)$/, scope: 'center' },
  { pattern: /^(?:home\/(?:progress|history))$/, scope: 'node' },
  { pattern: /^(?:events)$/, scope: 'node' },
  { pattern: /^(?:attention(?:\/(?:sync|changes|history))?)$/, scope: 'node' },
  { pattern: /^(?:operators)$/, scope: 'node' },
  {
    pattern:
      /^(?:shops(?:\/operators|\/import|\/tile|\/windows|\/\d+(?:\/(?:maximize|move-node|start|stop|qr-login|browser|show|hide|debug-on|debug-off|retry|home|orders|ad_logs|ad_products|ad_reports|goodsdata|transactions|overview|products|tickets|reviews|packages|invoices|aftersales|evidence|intercepts|navigator|quality|brands|activities|reconciliation_day|reconciliation_month|screenshot|input))?)?)$/,
    scope: 'node',
  },
  { pattern: /^(?:browsers)$/, scope: 'node' },
  { pattern: /^(?:logs)$/, scope: 'node' },
  {
    pattern:
      /^(?:system\/(?:status|summary|performance|progress|sources|settings|concurrency|home-collection|clear-queue|maintenance|backup|backup-verify|health|priority|retry-scope))$/,
    scope: 'node',
  },
  { pattern: /^(?:todos(?:\/sync)?)$/, scope: 'node' },
  { pattern: /^(?:product\/\d+\/\d+(?:\/sync)?)$/, scope: 'node' },
  { pattern: /^(?:tax-review\/\d+\/\d{4}-\d{2})$/, scope: 'node' },
  {
    pattern:
      /^(?:data\/(tax|orders|ad_logs|ad_products|ad_reports|goodsdata|transactions|overview|products|tickets|reviews|packages|invoices|aftersales|evidence|intercepts|navigator|quality|brands|activities|reconciliation_day|reconciliation_month)(?:\/sync)?)$/,
    scope: 'node',
  },
];

export function classifyRoute(route) {
  if (typeof route !== 'string' || route.length > 2048 || route.split('/').some((p) => !p || p === '.' || p === '..'))
    return null;
  const explicit = /^nodes\/\d+\/proxy\/(.+)$/.exec(route);
  if (explicit) return !explicit[1].startsWith('nodes/') && classifyRoute(explicit[1]) ? { scope: 'center' } : null;
  return routes.find((entry) => entry.pattern.test(route)) || null;
}
