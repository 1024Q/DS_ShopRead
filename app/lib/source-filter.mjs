import { sourceIssue, sourceActive } from './source-retry.mjs';
export const sourceFilterHelp = {
  all: '全部店铺业务模块，包含未读取和已停止的数据源。',
  active: '正在读取或已进入采集队列；点击不会启动采集。',
  issues: '读取失败、暂停重试或等待登录验证；已停止店铺不计入。',
  ready: '本地曾保存过数据，可能已过期或上传未完成。',
};
export function filterSources(
  snapshot,
  { shop = '', kind = '', cadence = '', state = '', keyword = '', sort = {}, page = 1 } = {},
) {
  const all = snapshot.rows || [],
    shops = new Set(
      String(shop || '')
        .split(',')
        .filter(Boolean),
    ),
    word = keyword.trim().toLocaleLowerCase();
  const rows = all.filter(
    (s) =>
      (!shops.size || shops.has(String(s.shop_id))) &&
      (!kind || s.kind === kind) &&
      (!cadence ||
        (cadence === 'off'
          ? s.cadence === 'off'
          : (s.queues?.length ? s.queues : [s.queue || (s.scope?._attention ? 'focus' : 'normal')]).includes(
              cadence,
            ))) &&
      (!state ||
        (state === 'active'
          ? sourceActive(s)
          : state === 'issues'
            ? sourceIssue(s)
            : state === 'recorded'
              ? !!s.updated_at
              : s.data_status === state)) &&
      (!word || JSON.stringify(s).toLocaleLowerCase().includes(word)),
  );
  const rank = (s) => (sourceIssue(s) ? 0 : sourceActive(s) ? 1 : s.data_status === 'partial' ? 2 : 3);
  const value = (s, k) =>
    k === 'scope' ? JSON.stringify(s.scope || {}) : Array.isArray(s[k]) ? s[k].join('、') : (s[k] ?? '');
  rows.sort((a, b) => {
    if (sort.field) {
      const av = value(a, sort.field),
        bv = value(b, sort.field);
      const cmp =
        typeof av === 'number' && typeof bv === 'number'
          ? av - bv
          : String(av).localeCompare(String(bv), 'zh-CN', { numeric: true });
      if (cmp) return sort.direction === 'desc' ? -cmp : cmp;
    }
    return rank(a) - rank(b) || a.shop_id - b.shop_id || a.kind.localeCompare(b.kind);
  });
  const eligible = rows.filter(
    (s) =>
      sourceIssue(s) &&
      (s.failed_scope_count || s.failed_scopes?.length || !sourceActive(s)) &&
      !s.waiting_login &&
      s.state !== 'stopped',
  );
  const pages = Math.max(1, Math.ceil(rows.length / 30)),
    current = Math.min(Math.max(1, page), pages);
  return {
    ...snapshot,
    rows: rows.slice((current - 1) * 30, current * 30),
    page: current,
    page_count: pages,
    total: rows.length,
    retry_count: eligible.reduce((n, s) => n + (s.failed_scope_count || s.failed_scopes?.length || 1), 0),
    waiting: rows.filter((s) => s.waiting_login).length,
    counts: {
      all: all.length,
      active: all.filter(sourceActive).length,
      issues: all.filter(sourceIssue).length,
      ready: all.filter((s) => s.updated_at).length,
    },
  };
}
