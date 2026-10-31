const tones = {
  待付款: 'amber',
  待发货: 'orange',
  待收货: 'blue',
  已收货: 'green',
  '退款/售后': 'purple',
  即将逾期发货: 'red',
  待处理即将逾期: 'red',
  在售中: 'green',
  报名成功: 'green',
  已签收: 'green',
  开票成功: 'green',
  已完结: 'green',
  拦截成功: 'green',
  已下架: 'gray',
  已关闭: 'gray',
  已处理: 'gray',
  暂不开票: 'gray',
  已撤销拦截: 'gray',
  非生效: 'gray',
  其他商品: 'gray',
  已售罄: 'amber',
  待处理: 'amber',
  待开票: 'amber',
  待商家处理: 'amber',
  待拦截: 'amber',
  揽件未中转: 'amber',
  预警商品: 'amber',
  无黑标商品: 'amber',
  发货未揽件: 'orange',
  待举证: 'orange',
  处理中: 'blue',
  拦截中: 'blue',
  中转未派件: 'blue',
  生效中: 'blue',
  待审核: 'blue',
  派件未签收: 'purple',
  平台处理中: 'purple',
  违规已处理: 'purple',
  已举证: 'teal',
  暂不支持拦截: 'gray',
  平台介入待商家处理: 'red',
  平台接入待商家处理: 'red',
  '2小时内将逾期': 'red',
  '3天内需开票': 'orange',
  拦截失败: 'red',
  开票失败: 'red',
  超时未举证: 'red',
  '即将发货-揽收超时': 'red',
  品质违规商品: 'red',
};
export function statusTone(value) {
  const text = String(value || '').trim();
  if (tones[text]) return tones[text];
  if (/失败|拒绝|驳回|超时|逾期|违规|异常/.test(text)) return 'red';
  if (/暂停|关闭|取消|撤销|结束|下架|失效/.test(text)) return 'gray';
  if (/成功|完成|通过|已签收|已回复|已登录/.test(text)) return 'green';
  if (/审核|处理中|进行中|推广中|投放中/.test(text)) return 'blue';
  if (/待|未回复|未处理/.test(text)) return 'amber';
  return 'gray';
}
export const DEFAULT_STATUSES = { orders: '待发货', products: '在售中', activities: '报名成功', invoices: '待开票' };
export function restoreStatus(kind, saved, initial) {
  if (initial) return initial;
  const draft = saved?.draft || {};
  return !saved?.statusDefaultsVersion && ['products', 'activities'].includes(kind) && !draft.status
    ? { ...draft, status: DEFAULT_STATUSES[kind] }
    : draft;
}
