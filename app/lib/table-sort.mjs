export const fieldsByKind = {
  orders: ['shop_name', 'name', 'status', 'amount', 'quantity', 'created_at', 'deadline', 'tracking_number'],
  products: ['shop_name', 'name', 'status', 'price_min', 'stock', 'sales', 'sales_30d', 'created_at'],
  tickets: ['shop_name', 'order_number', 'problem', 'status', 'progress', 'created_at', 'deadline'],
  reviews: ['shop_name', 'order_number', 'stars', 'content', 'reply', 'created_at'],
  ad_logs: ['shop_name', 'created_at', 'operator_name', 'name', 'status', 'operation_module', 'content'],
  activities: ['shop_name', 'name', 'status', 'activity_name', 'activity_type', 'price_min', 'created_at'],
  packages: ['shop_name', 'tracking_number', 'status', 'problem', 'pickup_deadline', 'carrier', 'created_at'],
  invoices: [
    'shop_name',
    'name',
    'status',
    'amount',
    'invoice_title',
    'invoice_title_type',
    'company_name',
    'created_at',
    'deadline',
  ],
  aftersales: ['shop_name', 'name', 'status', 'amount', 'tracking_number', 'created_at'],
  evidence: ['shop_name', 'order_number', 'status', 'amount', 'created_at', 'deadline'],
  intercepts: ['shop_name', 'name', 'status', 'tracking_number', 'progress'],
  navigator: ['shop_name', 'name', 'status', 'rating_rank', 'refund_rate', 'help_rate', 'sentiment_score', 'stat_date'],
  quality: ['shop_name', 'name', 'status', 'problem', 'negative_count', 'bad_count', 'progress'],
  brands: ['shop_name', 'name', 'status', 'problem', 'stock', 'stat_date'],
  tax: ['shop_name', 'name', 'period', 'trade_income', 'coupon_subsidy', 'refund', 'coupon_refund', 'tax_amount'],
  reconciliation_day: [
    'shop_name',
    'period',
    'income',
    'income_count',
    'outcome',
    'outcome_count',
    'net',
    'opening_balance',
    'closing_balance',
  ],
};
fieldsByKind.reconciliation_month = fieldsByKind.reconciliation_day;
const collator = new Intl.Collator('zh-CN', { numeric: true });
export function compareValues(a, b, direction = 'asc') {
  const empty = (v) => v == null || ['', '—', '未提供', '未知'].includes(String(v).trim());
  if (empty(a) || empty(b)) return empty(a) === empty(b) ? 0 : empty(a) ? 1 : -1;
  const clean = (v) =>
    String(v)
      .trim()
      .replace(/[¥￥,%元\s]/g, '');
  const x = clean(a),
    y = clean(b);
  const numeric = (v) => /^-?\d+(\.\d+)?$/.test(v) && v.replace(/[^0-9]/g, '').length < 16;
  const n = numeric(x) && numeric(y) ? Number(x) - Number(y) : collator.compare(String(a), String(b));
  return direction === 'desc' ? -n : n;
}
export function sortedRows(rows, sort) {
  return sort?.field ? [...rows].sort((a, b) => compareValues(a[sort.field], b[sort.field], sort.direction)) : rows;
}
