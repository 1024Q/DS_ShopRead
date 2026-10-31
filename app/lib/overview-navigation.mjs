// Explicitly clear remembered filters so dashboard counts open the right scope.
export function overviewTarget(metric, scope = {}) {
  const routes = {
    pending_shipping: ['orders', '待发货'],
    shipping_due: ['orders', '即将逾期发货'],
    pending_aftersales: ['aftersales', '待商家处理'],
    aftersales_due: ['aftersales', '待处理即将逾期'],
    pending_tickets: ['tickets', '待处理'],
  };
  if (!routes[metric]) return null;
  const [kind, status] = routes[metric];
  return {
    kind,
    filters: {
      ...scope,
      status,
      keyword: '',
      date_from: '',
      date_to: '',
      priority_only: false,
      exact_dates: false,
      sort_by: '',
      sort_direction: 'desc',
      carrier: '',
      stars: [],
      reply: '',
      min_price: '',
      max_price: '',
      low_stock: '',
      activity_type: '',
      activity_group: '',
      brand_reason: '',
      subject: '',
      period: '',
      trade_range: '',
    },
  };
}
