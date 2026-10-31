// Default column widths by meaning, so every list reads the same way: wide text columns
// for goods/orders/content, compact numbers and statuses, fixed room for dates and actions.
// Users can still drag a column wider; saved widths override these defaults.
const rules = [
  [/^操作$/, 140],
  [/运营|负责人/, 110],
  [/状态$/, 130],
  // Counts such as 订单数 / 退款订单数（实时口径） are numbers, not order text.
  [/数(（[^）]*）)?$/, 120],
  [/商品|订单|内容|信息|问题|详情|原因|物流|主体|抬头|对象|回复|活动/, 260],
  [/时间|日期|期限|账期|月份/, 160],
  [/数量|库存|评分|笔数|销量|次数/, 96],
  [/金额|价|元|费|率|ROI|余额|收入|支出|收益/, 130],
  [/状态|类型|分类|模块/, 130],
];
export function columnWidth(label, index) {
  if (index === 0) return 210;
  const text = typeof label === 'string' ? label : '';
  for (const [pattern, width] of rules) if (pattern.test(text)) return width;
  return 150;
}
