'use client';
import StatusBadge from '../shared/StatusBadge';
function Fields({ items }) {
  return (
    <dl>
      {items.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value === null || value === undefined || value === '' ? '—' : value}</dd>
        </div>
      ))}
    </dl>
  );
}
export default function InvoiceDetail({ row }) {
  const pdd = (row.platform || 'pdd') === 'pdd';
  return (
    <div className="invoice-details">
      <h3>开票申请</h3>
      <Fields
        items={[
          ['所属公司主体', row.company_name],
          ['订单编号', row.order_number],
          ['申请流水号', row.id],
          ['开票状态', <StatusBadge key="status" value={row.status} />],
          ['申请开票金额', row.amount == null ? null : `¥${Number(row.amount).toFixed(2)}`],
          ['申请时间', row.created_at],
          ['开票期限', row.deadline],
          ['失败 / 驳回原因', row.problem],
        ]}
      />
      <h3>购买方开票信息</h3>
      {pdd && !row.invoice_details_version && <p className="data-note">此记录尚未采集完整开票信息，重新采集后显示。</p>}
      <Fields
        items={[
          ['抬头类型', row.invoice_title_type || '未读取'],
          ['发票抬头', row.invoice_title],
          ['购买方税号', row.buyer_taxpayer_number],
          ['开户银行', row.buyer_bank],
          ['银行账号', row.buyer_bank_account],
          ['购买方地址', row.buyer_address],
          ['购买方电话', row.buyer_phone],
        ]}
      />
      {row.invoice_other_info && (
        <details className="invoice-original">
          <summary>平台其他开票信息原文</summary>
          <pre>{row.invoice_other_info}</pre>
        </details>
      )}
      <h3>商品与已开发票</h3>
      <Fields
        items={[
          ['商品名称', row.name],
          ['商品规格', row.goods_spec],
          ['商品数量', row.quantity],
          ['商家 SKU 编码', row.sku_code],
          ['发票号码', row.invoice_number],
          ['发票代码', row.invoice_code],
          ['开票时间', row.invoiced_at],
          ['上传时间', row.uploaded_at],
        ]}
      />
      {[row.paper_receiver_name, row.paper_receiver_mobile, row.paper_receiver_address, row.paper_tracking_number].some(
        Boolean,
      ) && (
        <>
          <h3>纸质发票寄送</h3>
          <Fields
            items={[
              ['收件人', row.paper_receiver_name],
              ['收件电话', row.paper_receiver_mobile],
              ['收件地址', row.paper_receiver_address],
              ['寄送单号', row.paper_tracking_number],
            ]}
          />
        </>
      )}
    </div>
  );
}
