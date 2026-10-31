import StatusBadge from '../shared/StatusBadge';
import SortableTable from '../shared/SortableTable';
const money = (v) => (v == null ? '—' : `¥${Number(v).toFixed(2)}`);
export default function ActivityDetail({ row }) {
  return (
    <section className="activity-detail">
      <h3>活动报名信息</h3>
      <dl>
        {[
          ['活动名称', row.activity_name],
          ['活动 ID', row.activity_id],
          ['活动类型', row.activity_type],
          ['报名状态', row.status],
          ['提交时间', row.created_at],
          ['报名记录编号', row.id],
          ['原因 / 说明', row.problem || '平台未提供'],
          ['活动说明', row.activity_description || '平台未提供'],
          ['外场活动价下限', money(row.outer_price_min)],
        ].map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{label === '报名状态' ? <StatusBadge value={value} /> : value || '—'}</dd>
          </div>
        ))}
      </dl>
      <h3>活动排期与价格</h3>
      <p className="data-note">排期为平台返回的报名计划；是否实际生效请结合报名状态。价格单位为元。</p>
      <div className="data-table-wrap">
        <SortableTable className="data-table">
          <thead>
            <tr>
              {['开始时间', '结束时间', '最低活动价', '最高活动价', '活动数量'].map((t) => (
                <th key={t}>{t}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(row.schedule || []).map((s, i) => (
              <tr key={i}>
                <td>{s.start || '未提供'}</td>
                <td>{s.end || '未提供'}</td>
                <td>{money(s.price_min)}</td>
                <td>{money(s.price_max)}</td>
                <td>{s.quantity ?? '未提供'}</td>
              </tr>
            ))}
          </tbody>
        </SortableTable>
        {!row.schedule?.length && <p className="data-note">平台未提供活动排期。</p>}
      </div>
      <h3>报名规格价格</h3>
      <div className="data-table-wrap">
        <SortableTable className="data-table">
          <thead>
            <tr>
              <th>规格</th>
              <th>SKU ID</th>
              <th>报名价</th>
            </tr>
          </thead>
          <tbody>
            {(row.skus || []).map((s, i) => (
              <tr key={`${s.sku_id}:${i}`}>
                <td>{s.name || '未提供'}</td>
                <td>{s.sku_id || '未提供'}</td>
                <td>{money(s.price)}</td>
              </tr>
            ))}
          </tbody>
        </SortableTable>
        {!row.skus?.length && <p className="data-note">平台未提供规格价格。</p>}
      </div>
    </section>
  );
}
