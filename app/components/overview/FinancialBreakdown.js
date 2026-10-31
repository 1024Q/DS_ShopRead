import { shopLabel } from '../../lib/shop-identity.mjs';
import SortableTable from '../shared/SortableTable';
const money = (value) =>
  '¥' + Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export default function FinancialBreakdown({ row }) {
  if (!row.breakdown) return <p className="data-note">尚未读取分类明细，请点击立即刷新。</p>;
  return (
    <div className="financial-breakdown">
      <p className="data-note">
        {shopLabel(row)} · {row.period} · 分类金额和笔数已与汇总核对，已保存。
        {row.details_through && `统计截至 ${row.details_through}`}
      </p>
      <div className="financial-breakdown-columns">
        {[
          ['income', '收入'],
          ['outcome', '支出'],
        ].map(([side, title]) => (
          <section key={side}>
            <h3>{title}分类明细</h3>
            <div className="data-table-wrap">
              <SortableTable className="data-table">
                <thead>
                  <tr>
                    <th>收支科目</th>
                    <th>{title}（元）</th>
                    <th>{title}笔数</th>
                  </tr>
                </thead>
                <tbody>
                  {row.breakdown[side].map((item) => (
                    <tr key={item.finance_id}>
                      <td>{item.label}</td>
                      <td className="finance-amount">{money(item.amount)}</td>
                      <td>{item.count.toLocaleString('zh-CN')}</td>
                    </tr>
                  ))}
                  {!row.breakdown[side].length && (
                    <tr>
                      <td colSpan={3}>本账期无{title}记录</td>
                    </tr>
                  )}
                </tbody>
                <tfoot>
                  <tr>
                    <th>合计</th>
                    <td className="finance-amount">{money(row[side])}</td>
                    <td>{row[side + '_count'].toLocaleString('zh-CN')}</td>
                  </tr>
                </tfoot>
              </SortableTable>
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
