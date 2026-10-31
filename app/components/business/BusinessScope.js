import { NativeButton } from '../shared/AdminControls';
import MultiSelect from '../shared/MultiSelect';
import { inScope, selectedValues } from '../../lib/filter-scope.mjs';
import PlatformFilter from '../shared/PlatformFilter';
import { platforms, exclusivePlatforms, canReadKind } from '../../lib/platforms.mjs';
import { moduleNames } from '../shell/WorkspaceTools';
export function BusinessScope({ view, platform, onChange, shops }) {
  const exclusive = exclusivePlatforms[view],
    rows = shops.filter((s) => inScope(platform, s.platform || 'pdd')),
    pending = rows.filter((s) => !canReadKind(s, view)).length;
  return (
    <div className="business-scope">
      {exclusive ? (
        <span className="platform-feature-tag">{platforms[exclusive]}专属功能</span>
      ) : (
        <label>
          平台筛选
          <MultiSelect
            label="业务平台筛选"
            value={platform}
            onChange={onChange}
            options={Object.entries(platforms).map(([value, label]) => ({ value, label }))}
            placeholder="全部平台"
          />
        </label>
      )}
      <span
        className="business-scope-note"
        title={
          exclusive
            ? '按该平台的规则与字段展示'
            : pending
              ? `${rows.length} 家店铺 · ${pending} 家业务数据待接入，统计仅包含已接入数据`
              : '统一功能入口 · 按店铺所属平台读取数据'
        }
      >
        {exclusive
          ? '按该平台的规则与字段展示'
          : pending
            ? `${rows.length} 家店铺 · ${pending} 家业务数据待接入，统计仅包含已接入数据`
            : '统一功能入口 · 按店铺所属平台读取数据'}
      </span>
    </div>
  );
}
export function PendingBusiness({ view, platform, onManage }) {
  return (
    <section className="business-panel pending-business">
      <div className="business-heading">
        <div>
          <h2>{moduleNames[view] || '重点待办'}</h2>
          <p>
            {selectedValues(platform)
              .map((p) => platforms[p])
              .join('、')}{' '}
            · 业务数据待接入
          </p>
        </div>
      </div>
      <div className="filter-panel">
        <PlatformFilter />
      </div>
      <div className="empty">
        <h3>此平台的数据采集尚未接入</h3>
        <p>该功能使用统一入口，接入后在这里查看店铺数据。当前可管理店铺、扫码登录和远程控制。</p>
        <NativeButton onClick={onManage}>管理店铺</NativeButton>
      </div>
    </section>
  );
}
