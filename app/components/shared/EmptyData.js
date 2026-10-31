'use client';
export default function EmptyData({ loading, error, sources = [], onRetry, onSync }) {
  const waiting = sources.some((s) => s.syncing),
    login = sources.some((s) => s.waiting_login || s.state === 'needs_verification'),
    failed = error || sources.some((s) => s.error),
    missing = sources.some((s) => !s.updated_at);
  const unsupported = sources.length > 0 && sources.every((s) => s.error_kind === 'unsupported' || s.pending_adapter),
    denied = sources.some((s) => s.error_kind === 'permission');
  const message = unsupported
    ? '所选平台尚不支持此模块或日期范围'
    : denied
      ? '部分店铺缺少平台业务权限，查看同步详情处理'
      : loading
        ? '正在读取已保存数据…'
        : waiting
          ? '采集已排队，入库后自动更新'
          : login
            ? '部分店铺需要完成登录验证'
            : failed
              ? '数据暂未读取成功，可重试或查看同步详情'
              : missing
                ? '当前范围尚无已保存数据'
                : '当前筛选没有匹配记录';
  return (
    <div className="table-empty" role="status">
      <p>{message}</p>
      {!loading && (
        <div className="empty-actions">
          {failed && onRetry && <button onClick={onRetry}>重新读取</button>}
          {missing && !waiting && !login && onSync && <button onClick={onSync}>采集当前范围</button>}
          {(failed || waiting || login || missing) && (
            <button
              onClick={() =>
                window.dispatchEvent(
                  new CustomEvent('workspace-navigate', { detail: { view: login ? 'shops' : 'sources' } }),
                )
              }
            >
              {login ? '前往店铺验证' : '查看同步详情'} ↗
            </button>
          )}
        </div>
      )}
    </div>
  );
}
