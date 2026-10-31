export const officialFields = [
  ['commission_base', '小红书计佣基数合计'],
  ['settlement_net', '小红书月度净收益'],
  ['goods_income', '货物收入总额'],
  ['goods_refund', '货物退款'],
  ['goods_net', '货物收入净额'],
  ['service_income', '服务收入总额'],
  ['service_refund', '服务退款'],
  ['service_net', '服务收入净额'],
  ['platform_commission', '平台佣金/服务费'],
  ['commission_no_ads', '不含广告费用'],
  ['ads_non_union', '非京东联盟广告费'],
  ['ads_union', '京东联盟广告费'],
];
export function detailFields(row) {
  const platform = row.platform || row.shop_breakdown?.[0]?.platform;
  const xhs = platform === 'xiaohongshu' || (!platform && row.tax_source === '小红书月度结算账单');
  const excluded = new Set(
    xhs
      ? ['service_income', 'service_refund', 'service_net', 'commission_no_ads', 'ads_non_union', 'ads_union']
      : [
          'commission_base',
          'settlement_net',
          ...(platform === 'jd' ? [] : ['commission_no_ads', 'ads_non_union', 'ads_union']),
        ],
  );
  return officialFields.filter(([key]) => !excluded.has(key));
}
