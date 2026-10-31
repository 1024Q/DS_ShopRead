const registry = require('../config/platforms.json');
const pages = Object.fromEntries([
  ...Object.values(registry).flatMap((p) => p.groups.flatMap(([, items]) => items)),
  ['overview', '实时概览'],
  ['todos', '重点待办'],
  ['cost_tables', '成本表'],
  ['report_exports', '报表导出'],
  ['report_view', '详细报表'],
  ['promotion_ledger', '多多推广流水'],
  ['profit', '利润核算'],
  ['profit_tax', '税费预估与核对'],
  ['fleet', '跨机器汇总'],
  ['shops', '店铺管理'],
  ['browser_workspace', '店铺浏览器'],
  ['sources', '同步与数据源'],
  ['machines', '远程机器'],
  ['settings', '系统设置'],
  ['logs', '运行日志'],
]);
function can(user, page, write = false) {
  if (!user || user.disabled) return false;
  if (page === 'accounts') return user.role === 'admin';
  if (!Object.hasOwn(pages, page)) return false;
  return (
    user.role === 'admin' || user.permissions?.[page] === 'write' || (!write && user.permissions?.[page] === 'read')
  );
}
function firstPage(user) {
  return Object.keys(pages).find((p) => can(user, p)) || '';
}
function allowed(user, route, method = 'GET', search = '') {
  if (!user || user.disabled) return false;
  if (user.role === 'admin') return true;
  route = route.replace(/^nodes\/\d+\/proxy\//, '');
  const write = !['GET', 'HEAD'].includes(method);
  const any = (names, mutation = write) => names.some((p) => can(user, p, mutation));
  // Shared display metadata never authorizes a mutation or a business query.
  if (['shops', 'operators', 'fleet', 'nodes'].includes(route) && !write) return !!firstPage(user);
  if (route === 'events' && !write) return !!firstPage(user); // Only revision, kind, shop id and platform; no business payload.
  if (route === 'home/snapshot') return any(['overview', 'goodsdata']);
  if (/^home\/(progress|history)$/.test(route)) return can(user, 'overview', write);
  if (/^(attention|todos)(\/|$)/.test(route)) return any(['todos']);
  const data = /^data\/([^/]+)(?:\/.*)?$/.exec(route);
  if (data) return any([data[1] === 'reconciliation_month' ? 'reconciliation_day' : data[1]]);
  if (/^report-exports\/view$/.test(route)) return any(['report_view']);
  if (/^report-exports\/unmatched$/.test(route)) return any(['cost_tables', 'report_view', 'profit']);
  if (/^report-exports(?:\/|$)/.test(route)) {
    if (!write && /\/(download|screenshot)$/.test(route)) return any(['report_exports', 'report_view']);
    if (!write && route === 'report-exports') return any(['report_exports', 'report_view', 'profit']);
    return any(['report_exports']);
  }
  if (/^(cost-tables|cost-supplements)(\/|$)/.test(route)) return any(['cost_tables']);
  if (/^profit-tax(\/|$)/.test(route)) return any(['profit_tax']);
  if (/^profit(\/|$)/.test(route)) return any(['profit']);
  if (route === 'promotion-ledger') return any(['promotion_ledger']);
  if (/^tax-review\//.test(route)) return any(['tax']);
  if (/^product\//.test(route)) return any(['products', 'goodsdata']);
  if (/^(invoice-imports|invoices\/refresh-pending)/.test(route)) return any(['invoices']);
  // Generated exports are shared handles and may contain another page's data.
  if (route === 'table-tools/orders') return can(user, 'report_view'); // POST is a filtered read, not a mutation.
  if (/^table-tools\/cost\/[a-f0-9]{32}$/.test(route)) return any(['cost_tables']);
  if (route === 'table-tools/exports') return !!firstPage(user); // Scoped creation and list filtering also run in the proxy.
  if (/^(exports|table-tools)\//.test(route)) return false;
  if (route === 'logs') return any(['logs']);
  if (/^system\/(settings|concurrency|priority|retry-scope|clear-queue)$/.test(route))
    return any(['settings', 'sources']);
  if (route === 'system/home-collection') return any(['overview', 'sources']);
  if (/^system\/(status|summary|performance|progress|sources|health)$/.test(route))
    return any(['sources', 'fleet', 'settings']);
  if (route === 'browsers') return any(['browser_workspace']);
  const shop = /^shops\/\d+(?:\/([^/]+))?$/.exec(route);
  if (shop) {
    if (shop[1] && Object.hasOwn(pages, shop[1])) return any([shop[1]]);
    if (shop[1] === 'refund-actions') return any(['todos']);
    return any(['shops', 'browser_workspace']);
  }
  return false;
}
module.exports = { pages, can, firstPage, allowed };
