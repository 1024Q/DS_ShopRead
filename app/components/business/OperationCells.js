import StatusBadge from '../shared/StatusBadge';
import { pickupCountdown } from '../../lib/package-countdown.mjs';
import { ProductInfo } from '../shared/ProductImage';
export { operationHeaders } from '../../lib/operation-headers.mjs';
export const operationTitles = {
  orders: '订单查询',
  ad_logs: '操作记录',
  activities: '活动报名记录',
  tax: '报税信息',
  reconciliation_day: '账户资金 · 对账中心',
  reconciliation_month: '账户资金 · 对账中心',
  packages: '包裹中心',
  invoices: '订单开票',
  aftersales: '售后工作台',
  evidence: '商家举证',
  intercepts: '拦截快递',
  navigator: '商品领航员',
  quality: '品质诊断',
  brands: '品牌黑标权益',
};
export const operationStatuses = {
  orders: ['待付款', '待发货', '待收货', '退款/售后', '即将逾期发货', '退款成功'],
  activities: ['报名成功'],
  aftersales: ['平台介入待商家处理', '平台处理中', '待商家处理', '待处理即将逾期'],
  evidence: ['待举证', '超时未举证', '已举证', '已处理'],
  intercepts: ['拦截中', '拦截失败', '拦截成功', '待拦截', '暂不支持拦截', '已撤销拦截'],
  invoices: ['待开票', '开票成功', '开票失败', '暂不开票'],
  quality: ['预警商品', '品质违规商品', '其他商品'],
  brands: ['无黑标商品'],
};
export const operationNotes = {
  orders: '按订单状态查询；退款/售后按售后申请时间查询，金额为退款金额。',
  ad_logs: '近7天的商品推广、直播推广、明星店铺、整合营销及账户操作。',
  activities: '仅含近期报名记录，不含已归档记录；报名成功不等于活动已生效。',
  tax: '同一主体按月合并；当月及本季度仅含平台已发布数据。',
  reconciliation_day: '日账单次日更新；收益包含提现等账务变动。',
  reconciliation_month: '当月账单为截至平台统计日期的累计值。',
  aftersales: '默认近七天，另含不限日期的平台介入及平台处理中记录。',
  intercepts: '同一订单的多个快递分别计数。',
  navigator: '按平台统计周期更新，并非当日实时指标。',
};
const fmt = (v) => (v == null ? '—' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 }));
const money = (v) => (v == null ? '—' : `¥${Number(v).toFixed(2)}`);
const rate = (v) => (v == null ? '—' : `${fmt(v)}%`);
function Product({ row }) {
  return (
    <td className="wide-cell">
      <ProductInfo row={row} />
    </td>
  );
}
function Status({ row }) {
  return (
    <td>
      <StatusBadge value={row.status} />
      {row.priority >= 2 && <small className="priority-label">{row.priority_reason || '优先关注'}</small>}
    </td>
  );
}
export default function OperationCells({ kind, row, now }) {
  const countdown = kind === 'packages' ? pickupCountdown(row, now) : null;
  if (kind.startsWith('reconciliation_'))
    return (
      <>
        <td className="date-cell">
          <strong>{row.period}</strong>
          {row.summary_through && <small>截至 {row.summary_through}</small>}
        </td>
        <td className="finance-amount">{money(row.income)}</td>
        <td>{fmt(row.income_count)}</td>
        <td className="finance-amount">{money(row.outcome)}</td>
        <td>{fmt(row.outcome_count)}</td>
        <td className="finance-amount">{money(row.net)}</td>
        <td className="finance-amount">{money(row.opening_balance)}</td>
        <td className="finance-amount">{money(row.closing_balance)}</td>
      </>
    );

  if (kind === 'orders')
    return (
      <>
        <Product row={row} />
        <td>
          <StatusBadge value={row.status} />
          {row.attention_status === '即将逾期发货' && (
            <small>
              <StatusBadge value={row.attention_status} />
            </small>
          )}
        </td>
        <td>{money(row.amount)}</td>
        <td>{fmt(row.quantity)}</td>
        <td className="date-cell">{row.created_at || '—'}</td>
        <td className="date-cell">{row.deadline || '—'}</td>
        <td>{row.tracking_number || '—'}</td>
      </>
    );
  if (kind === 'ad_logs')
    return (
      <>
        <td className="date-cell">{row.created_at || '—'}</td>
        <td>{row.operator_name || '—'}</td>
        <td className="wide-cell">{row.name || '—'}</td>
        <td>
          <StatusBadge value={row.status} />
        </td>
        <td>{row.operation_module}</td>
        <td className="wide-cell">
          <span className="clamp-text" title={row.content}>
            {row.content || '—'}
          </span>
        </td>
      </>
    );
  if (kind === 'activities')
    return (
      <>
        <Product row={row} />
        <td className="wide-cell">
          <StatusBadge value={row.status} />
          {row.problem && <small className="clamp-text">{row.problem}</small>}
        </td>
        <td className="wide-cell">
          <strong>{row.activity_name || '未提供活动名称'}</strong>
          <small>活动 ID：{row.activity_id || '未提供'}</small>
        </td>
        <td>{row.activity_type || '未提供'}</td>
        <td className="nowrap">
          {money(row.price_min)}
          {row.price_max !== row.price_min ? ' ~ ' + money(row.price_max) : ''}
          {row.schedule?.length > 1 && <small>含 {row.schedule.length} 段排期</small>}
        </td>
        <td className="date-cell">{row.created_at || '未提供'}</td>
      </>
    );
  if (kind === 'packages')
    return (
      <>
        <td className="identifier-cell">
          {(row.goods_id || row.name) && <ProductInfo row={row} />}
          <small>订单 {row.order_number}</small>
          <strong>运单 {row.tracking_number || '—'}</strong>
        </td>
        <Status row={row} />
        <td>
          {row.problem || '平台未提供'}
          <small>{row.progress}</small>
        </td>
        <td
          title={
            row.pickup_deadline
              ? `发货后 24 小时未揽收即超时；期限 ${row.pickup_deadline.replace('T', ' ')}`
              : undefined
          }
        >
          {countdown ? <StatusBadge value={countdown.text} tone={countdown.tone} countdown /> : '—'}
        </td>
        <td>{row.carrier || '—'}</td>
        <td className="date-cell">{row.created_at || '—'}</td>
      </>
    );
  if (kind === 'invoices')
    return (
      <>
        <td className="wide-cell">
          <ProductInfo row={row} showImage={false} />
        </td>
        <Status row={row} />
        <td>{money(row.amount)}</td>
        <td>{row.invoice_title || '—'}</td>
        <td>{row.invoice_title_type || '未读取'}</td>
        <td className="wide-cell">{row.company_name || '未读取主体'}</td>
        <td className="date-cell">{row.created_at || '—'}</td>
        <td className="date-cell">
          {row.deadline || '—'}
          {row.overdue && <small className="priority-label">已逾期</small>}
        </td>
      </>
    );
  if (kind === 'aftersales')
    return (
      <>
        <Product row={row} />
        <td>
          <StatusBadge value={row.status} />
          <small>{row.progress}</small>
        </td>
        <td>
          {row.row_type === 'package_warning' ? (
            <>
              预估赔偿 {money(row.estimated_claim)}
              <small>{row.problem}</small>
            </>
          ) : (
            <>
              {money(row.amount)}
              <small>{row.problem}</small>
            </>
          )}
        </td>
        <td className="identifier-cell">
          {row.tracking_number || '—'}
          <small>
            {row.carrier} {row.logistics_status}
          </small>
          {row.return_tracking_number && <small>退货 {row.return_tracking_number}</small>}
        </td>
        <td className="date-cell">
          {row.created_at || '—'}
          {row.warning_at && <small>预警：{row.warning_at}</small>}
        </td>
      </>
    );
  if (kind === 'evidence')
    return (
      <>
        <Product row={row} />
        <Status row={row} />
        <td>{money(row.amount)}</td>
        <td className="date-cell">{row.created_at || '—'}</td>
        <td className="date-cell">{row.deadline || '—'}</td>
      </>
    );
  if (kind === 'intercepts')
    return (
      <>
        <Product row={row} />
        <Status row={row} />
        <td className="identifier-cell">
          <strong>{row.tracking_number || '未提供单号'}</strong>
          <small>{row.carrier}</small>
        </td>
        <td className="wide-cell">
          <div className="clamp-text">{row.progress || '未提供物流'}</div>
          <small>{row.logistics_time}</small>
          {row.problem && <small>{row.problem}</small>}
        </td>
      </>
    );
  if (kind === 'navigator')
    return (
      <>
        <Product row={row} />
        <Status row={row} />
        <td>{rate(row.rating_rank)}</td>
        <td>{rate(row.refund_rate)}</td>
        <td>{rate(row.help_rate)}</td>
        <td>{fmt(row.sentiment_score)}</td>
        <td className="date-cell">{row.stat_date || '未提供'}</td>
      </>
    );
  if (kind === 'quality')
    return (
      <>
        <Product row={row} />
        <Status row={row} />
        <td className="wide-cell" title={row.problem || '本次平台结果未提供此字段，不代表没有问题'}>
          {row.problem || '字段未返回'}
        </td>
        <td>
          {fmt(row.negative_count)}
          <small>反馈率 {rate(row.negative_rate)}</small>
        </td>
        <td>
          {fmt(row.bad_count)}
          <small>差评率 {rate(row.bad_rate)}</small>
        </td>
        <td>
          <StatusBadge value={row.effect_status || '待更新'} />
          <small>{row.progress || '—'}</small>
          <small>{row.deadline || row.stat_date}</small>
        </td>
      </>
    );
  return (
    <>
      <Product row={row} />
      <Status row={row} />
      <td className="wide-cell" title={row.problem || '平台未返回原因，需打开后台核对'}>
        {row.problem || '原因未返回'}
      </td>
      <td>{fmt(row.stock)}</td>
      <td className="date-cell">{row.stat_date || '未提供'}</td>
    </>
  );
}
