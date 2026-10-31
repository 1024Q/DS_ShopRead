import SortableTable from '../shared/SortableTable';
import { shopLabel } from '../../lib/shop-identity.mjs';
const amount = (n) =>
  n == null ? '—' : '¥' + (n / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export default function TaxChangeActions({ shop, period }) {
  const c = shop.db_comparison || {},
    before = c.previous;
  const names = {
    trade_income: '交易收入',
    coupon_subsidy: '优惠券补贴',
    refund: '退款',
    coupon_refund: '退款退优惠券',
    tax_amount: '报税参考净额',
    goods_income: '货物收入总额',
    goods_refund: '货物退款',
    goods_net: '货物收入净额',
    service_income: '服务收入',
    service_refund: '服务退款',
    service_net: '服务收入净额',
    platform_commission: '平台佣金/服务费',
    commission_base: '计佣基数合计',
    settlement_net: '月度净收益',
    commission_no_ads: '不含广告费用',
    ads_non_union: '非京东联盟广告费',
    ads_union: '京东联盟广告费',
  };
  const entries = Object.entries(names).filter(([k]) => before?.[k + '_cents'] != null || shop[k + '_cents'] != null);
  return (
    <article className="tax-change-card">
      <header>
        <strong>{shopLabel(shop)}</strong>
        <span>{period}</span>
      </header>
      <p className="data-note">已自动采用最新成功采集值，仅保留上一版。金额减少、公司名称变化时高亮。</p>
      {!before ? (
        <p>暂无上一版历史值。</p>
      ) : (
        <div className="data-table-wrap">
          <SortableTable className="data-table">
            <thead>
              <tr>
                <th>字段</th>
                <th>上一版</th>
                <th>当前值</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <th>公司名称</th>
                <td>{before.entity?.company_name || '—'}</td>
                <td className={c.company_changed ? 'difference-cell' : ''}>{shop.entity?.company_name || '—'}</td>
              </tr>
              {entries.map(([k, label]) => (
                <tr key={k}>
                  <th>{label}</th>
                  <td>{amount(before[k + '_cents'])}</td>
                  <td
                    className={
                      c.differences?.some((d) => d.field === k + '_cents' && d.highlight) ? 'difference-cell' : ''
                    }
                  >
                    {amount(shop[k + '_cents'])}
                  </td>
                </tr>
              ))}
            </tbody>
          </SortableTable>
        </div>
      )}
    </article>
  );
}
