import TaxChangeActions from './TaxChangeActions';
import CollapsibleSection from '../shared/CollapsibleSection';
import { shopLabel } from '../../lib/shop-identity.mjs';
import SortableTable from '../shared/SortableTable';
export const chineseDate = (value) =>
  typeof value === 'string'
    ? value
        .replace(/^(\d{4})-Q([1-4])$/, '$1年第$2季度')
        .replace(
          /^(\d{4})-(\d{2})(?:-(\d{2}))?$/,
          (_, year, month, day) => `${year}年${Number(month)}月${day ? Number(day) + '日' : ''}`,
        )
    : value;
const money = (value) =>
  value == null
    ? '—'
    : '¥' + Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export { officialFields } from '../../lib/tax-detail-fields.mjs';
import { officialFields, detailFields } from '../../lib/tax-detail-fields.mjs';
export const taxHeaders = (official) => [
  '所属店铺',
  '主体信息',
  '账期',
  '交易收入（拼多多）',
  '优惠券补贴（拼多多）',
  '退款（拼多多）',
  '退款退优惠券（拼多多）',
  '报税参考净额',
  ...(official ? officialFields.map(([, label]) => label + '（元）') : []),
  '操作',
];
const fields = [
  ['trade_income', '交易收入'],
  ['coupon_subsidy', '优惠券补贴'],
  ['refund', '退款'],
  ['coupon_refund', '退款退优惠券'],
  ['tax_amount', '报税金额'],
];
const entityFields = [
  ['subject_type', '主体类型'],
  ['company_name', '公司名称'],
  ['credit_code', '统一社会信用代码'],
  ['taxpayer_number', '纳税人识别号'],
  ['legal_representative', '法定代表人'],
  ['company_type', '公司类型'],
  ['registration_status', '公司登记状态'],
  ['registered_address', '公司注册地址'],
  ['business_address', '公司经营地址'],
  ['license_start', '营业期限起'],
  ['license_end', '营业期限止'],
  ['business_scope', '经营范围'],
];

export function TaxSubjects({ subjects }) {
  return (
    <CollapsibleSection className="data-module">
      <h3>主体信息</h3>
      <div className="tax-subjects">
        {subjects.map((s) => (
          <article className="tax-subject" key={s.id}>
            <div className="tax-subject-heading">
              <strong>{(s.shop_labels || s.shop_names.map((name) => '平台未知 · ' + name)).join('、')}</strong>
              <span>主体信息</span>
            </div>
            <h3 className={s.company_changed ? 'difference-cell' : ''}>
              {s.entity.company_name}
              {s.company_changed && <small> 公司名称已变化</small>}
            </h3>
            <p>
              统一社会信用代码：<strong>{s.entity.credit_code || '平台未提供'}</strong>
            </p>
            <p>
              {s.entity.subject_type || s.entity.company_type || '—'} · {s.entity.registration_status || '—'}
            </p>
            <details>
              <summary>查看完整主体信息</summary>
              <EntityFields entity={s.entity} />
            </details>
          </article>
        ))}
      </div>
    </CollapsibleSection>
  );
}
function EntityFields({ entity }) {
  return (
    <dl className="tax-entity-fields">
      {entityFields.map(([key, label]) => (
        <div key={key}>
          <dt>{label}</dt>
          <dd>{chineseDate(entity?.[key]) || '平台未提供'}</dd>
        </div>
      ))}
    </dl>
  );
}
export function TaxSummary({ data }) {
  return (
    <CollapsibleSection className="data-module">
      <h3>报税金额汇总</h3>
      <p className="data-note">
        拼多多采用月账单四项计算口径；天猫、京东采用官方涉税报送账单；小红书采用月度结算账单订单结算的计佣基数合计（含退款负数）。天猫请选择季度，季度金额不拆分到月。报税参考净额不是应缴税额，平台费用单列展示。
      </p>
      {data?.tax_summary?.official_count > 0 && (
        <p>
          平台报税口径收入净额：<strong>{money(data.tax_summary.official_net)}</strong> ·{' '}
          {data.tax_summary.official_count} 条账单
        </p>
      )}
      <div className="tax-formula">
        <strong>报税金额 = 交易收入 + 优惠券补贴 − 退款 − 退款退优惠券</strong>
        <span>按你指定的四项口径计算；退款按扣减金额展示。数据来源：货款账户 → 月汇总 → 展开。</span>
      </div>
      <div className="metric-grid tax-metrics">
        {fields.map(([key, label]) => (
          <div className={`metric-card ${key === 'tax_amount' ? 'tax-total' : ''}`} key={key}>
            <span>
              {label}
              {key === 'tax_amount' ? '合计' : ''}
            </span>
            <strong>{money(data?.tax_summary?.[key])}</strong>
          </div>
        ))}
      </div>
      <details className="context-note">
        <summary>统计说明</summary>
        <p>
          合计包含所选月份 /
          季度的全部已读取账期，同一主体下店铺合并计算，不受分页影响。主体及金额已按店铺、月份存入数据库；成功采集后直接覆盖原值，仅保留上一版历史值。金额减少时高亮，公司名称变化时高亮；其他主体字段变化不高亮。未返回账期保留最后一次成功金额，不置零。
        </p>
      </details>
    </CollapsibleSection>
  );
}
export function TaxCells({ row, official = false }) {
  return (
    <>
      <td className="wide-cell">
        <strong>{row.entity?.company_name || '—'}</strong>
        <small>{row.entity?.credit_code || '统一社会信用代码未提供'}</small>
        {(row.shop_breakdown || []).some((s) =>
          s.db_comparison?.differences?.some((d) => d.field === 'company_name' && d.highlight),
        ) && <strong className="difference-badge">公司名称已变化</strong>}
        {row.current_month && !row.db_changed && <small className="tax-current-note">当月持续更新</small>}
        {row.db_missing && <small>平台未返回部分店铺账期，沿用数据库金额</small>}
      </td>
      <td className="date-cell">
        {chineseDate(row.period)}
        <small>截至 {chineseDate(row.summary_through)}</small>
        {row.partial && <small>账期尚未完整</small>}
        {row.official_tax && <small>{row.tax_source || '平台官方涉税报送账单'}</small>}
      </td>
      {fields.map(([key]) => (
        <td
          key={key}
          className={`finance-amount ${key === 'tax_amount' ? 'tax-value' : ''} ${(row.shop_breakdown || []).some((s) => s.db_comparison?.differences?.some((d) => d.field === key + '_cents' && d.highlight)) ? 'difference-cell' : ''}`}
        >
          {money(row[key])}
        </td>
      ))}
      {official &&
        officialFields.map(([key]) => (
          <td
            className={
              (row.shop_breakdown || []).some((s) =>
                s.db_comparison?.differences?.some((d) => d.field === key + '_cents' && d.highlight),
              )
                ? 'finance-amount difference-cell'
                : 'finance-amount'
            }
            key={key}
          >
            {money(row[key])}
          </td>
        ))}
    </>
  );
}
function DatabaseComparison({ row }) {
  const shops = row.shop_breakdown || [row];
  if (!shops.length) return <p className="data-note">已与数据库记录核对；当前采用店铺后台最新成功采集数据。</p>;
  return (
    <section className={`database-comparison ${row.current_month && !row.db_changed ? 'routine-comparison' : ''}`}>
      <h3>上一版历史值与当前值</h3>
      {shops.map((shop) => (
        <TaxChangeActions key={`${shop.shop_id}:${row.period}`} shop={shop} period={row.period} />
      ))}
    </section>
  );
}
export function TaxDetail({ row }) {
  if (row.official_tax)
    return (
      <>
        <DatabaseComparison row={row} />
        <p>
          {chineseDate(row.period)} · {row.tax_source || '平台官方涉税报送账单'}
        </p>
        <p>{row.tax_basis}</p>
        <dl className="tax-entity-fields">
          {[...detailFields(row), ['tax_amount', '报送收入净额合计']].map(([key, label]) => (
            <div key={key}>
              <dt>{label}</dt>
              <dd>{money(row[key])}</dd>
            </div>
          ))}
        </dl>
        {row.orders != null && <p>交易订单笔数：{row.orders}</p>}
        <h3>账单主体</h3>
        <EntityFields entity={row.entity} />
      </>
    );
  return (
    <>
      <p>
        入账月份：{chineseDate(row.period)} · 账单截至 {chineseDate(row.summary_through)}
        {row.partial ? '（本月尚未完整）' : ''}
      </p>
      <DatabaseComparison row={row} />
      <dl className="tax-entity-fields">
        {fields.map(([key, label]) => (
          <div key={key}>
            <dt>{label}</dt>
            <dd>{money(row[key])}</dd>
          </div>
        ))}
      </dl>
      <p className="tax-formula">
        {money(row.trade_income)} + {money(row.coupon_subsidy)} − {money(row.refund)} − {money(row.coupon_refund)} ={' '}
        {money(row.tax_amount)}
      </p>
      <h3>店铺金额明细</h3>
      <div className="data-table-wrap">
        <SortableTable className="data-table">
          <thead>
            <tr>
              <th>店铺</th>
              {fields.map(([key, label]) => (
                <th key={key}>{label}（元）</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(row.shop_breakdown || []).map((shop) => (
              <tr data-shop-id={shop.shop_id} key={shop.shop_id}>
                <td>{shopLabel(shop)}</td>
                {fields.map(([key]) => (
                  <td key={key}>{money(shop[key])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </SortableTable>
      </div>
      <h3>店铺主体信息</h3>
      <EntityFields entity={row.entity} />
    </>
  );
}
