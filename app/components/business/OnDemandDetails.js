'use client';
import { NativeButton } from '../shared/AdminControls';
import UpdatedAgo from '../shared/UpdatedAgo';
import { shopLabel } from '../../lib/shop-identity.mjs';

export default function OnDemandDetails({ sources = [], label, onCollect, busy = false }) {
  const selected = sources.filter((source) => source.details_on_demand);
  if (!selected.length) return null;
  return (
    <div className="notice" role="status">
      <p>首页汇总按原频率更新；{label}按需采集，保留上次已读取的数据。</p>
      <details>
        <summary>查看各店铺明细更新时间</summary>
        {selected.map((source) => (
          <p key={source.shop_id}>
            {shopLabel(source)} ·{' '}
            {source.details_updated_at ? <UpdatedAgo value={source.details_updated_at} /> : '尚未采集明细'}
            {source.details_updated_at && !source.details_complete ? ' · 部分明细' : ''}
          </p>
        ))}
      </details>
      {onCollect && (
        <NativeButton type="button" onClick={onCollect} disabled={busy}>
          采集当前筛选店铺的{label}
        </NativeButton>
      )}
    </div>
  );
}
