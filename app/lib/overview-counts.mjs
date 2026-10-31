export const overviewCountKeys = [
  'pending_shipping',
  'pending_aftersales',
  'pending_tickets',
  'shipping_due',
  'aftersales_due',
];
const platformKeys = new Set(['pending_shipping', 'pending_aftersales', 'pending_tickets']);
const valid = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));

// A complete list must be verified against its requested status and date scope
// by the server. An urgent-only snapshot is never evidence for the full backlog.
export function overviewCounts(rows, reports = {}) {
  const projected = rows.map((row) => {
    const result = { ...row, count_basis: {} };
    for (const metric of overviewCountKeys) {
      const report = reports[metric],
        saved = report?.home_counts?.rows?.find((value) => String(value.shop_id) === String(row.shop_id));
      const verified = !!saved?.verified && valid(saved.count);
      const platform = platformKeys.has(metric) && valid(row[metric]);
      const partial = !platformKeys.has(metric) && saved?.updated_at && valid(saved.count);
      // A full 90/180-day list still does not prove the platform's all-age card
      // has the same definition. Preserve those original numbers side by side.
      const value = platformKeys.has(metric)
        ? platform
          ? Number(row[metric])
          : null
        : verified
          ? Number(saved.count)
          : partial
            ? Number(saved.count)
            : null;
      const basis = platformKeys.has(metric)
        ? platform
          ? 'platform_home'
          : 'missing'
        : verified
          ? 'list'
          : partial
            ? 'partial_list'
            : 'missing';
      result[metric] = value;
      result.count_basis[metric] = {
        value,
        basis,
        updated_at: basis === 'platform_home' ? row.updated_at : saved?.updated_at,
        list_count: saved?.updated_at && valid(saved.count) ? Number(saved.count) : null,
        list_updated_at: saved?.updated_at,
        verified,
        list_freshness_status: saved?.freshness_status || 'unknown',
        list_freshness_reason: saved?.freshness_reason || '',
        last_received_at: saved?.last_received_at,
        reason: saved?.reason || '平台首页与列表的状态或日期口径可能不同',
        scope: report?.scope,
        snapshot_at: report?.home_counts?.snapshot_at,
      };
    }
    return result;
  });
  const metrics = Object.fromEntries(
    overviewCountKeys.map((metric) => {
      const values = projected.map((row) => row.count_basis[metric]),
        known = values.filter((value) => valid(value.value));
      const bases = new Set(known.map((value) => value.basis)),
        times = known
          .map((value) => value.updated_at)
          .filter(Boolean)
          .sort();
      return [
        metric,
        {
          value: known.length ? known.reduce((sum, value) => sum + value.value, 0) : null,
          basis: bases.size > 1 ? 'mixed' : bases.values().next().value || 'missing',
          missing: values.length - known.length,
          list_shops: values.filter((value) => value.basis === 'list').length,
          platform_shops: values.filter((value) => value.basis === 'platform_home').length,
          partial_shops: values.filter((value) => value.basis === 'partial_list').length,
          updated_at: times[0],
          scope: reports[metric]?.scope,
          list_count: values.some((value) => valid(value.list_count))
            ? values.reduce((sum, value) => sum + (valid(value.list_count) ? value.list_count : 0), 0)
            : null,
          list_freshness_counts: values
            .filter((value) => valid(value.list_count))
            .reduce(
              (counts, value) => ({
                ...counts,
                [value.list_freshness_status]: (counts[value.list_freshness_status] || 0) + 1,
              }),
              {},
            ),
          verified: values.length > 0 && values.every((value) => value.verified),
          list_updated_at: reports[metric]?.home_counts?.rows
            ?.map((row) => row.updated_at)
            .filter(Boolean)
            .sort()[0],
        },
      ];
    }),
  );
  return {
    rows: projected,
    metrics,
    totals: Object.fromEntries(overviewCountKeys.map((metric) => [metric, metrics[metric].value])),
  };
}

export function listStateLabel(value = {}) {
  const labels = {
    offline: '节点离线',
    paused: '定时已暂停',
    waiting_login: '等待登录',
    overdue: '超过设定频率',
    missing: '尚未读取',
    current: '频率内',
    unknown: '时效待核对',
  };
  const times = value.list_freshness_counts
    ? Object.entries(value.list_freshness_counts)
        .map(([state, count]) => `${count} 家${labels[state] || labels.unknown}`)
        .join(' · ')
    : value.list_freshness_reason || labels[value.list_freshness_status] || labels.unknown;
  return `${value.verified ? '范围完整' : '范围未完整'} · ${times}`;
}

export function countBasisLabel(value = {}) {
  const label =
    {
      list: '对应列表口径',
      platform_home: '平台首页口径',
      partial_list: '列表未读取完整',
      missing: '尚未读取',
      mixed: value.platform_shops ? '部分店铺使用平台首页口径' : '部分店铺列表未读取完整',
    }[value.basis] || '尚未读取';
  return label + (value.missing ? ` · ${value.missing} 家缺数据` : '');
}

export function countBasisDetail(value = {}) {
  const scope = value.scope || {};
  const range =
    ['待商家处理', '待处理即将逾期'].includes(scope.status) && !scope.explicit_dates
      ? '不限日期'
      : scope.date_from && scope.date_to
        ? `${scope.date_from} 至 ${scope.date_to}`
        : '';
  const time = value.updated_at ? new Date(value.updated_at).toLocaleString('zh-CN', { hour12: false }) : '尚未读取';
  const reason =
    value.reason || (value.verified ? '平台首页与列表的状态或日期口径可能不同' : '尚未取得全部店铺可核验的完整列表');
  const explanation =
    value.basis === 'list'
      ? '与对应列表使用相同筛选和本次数据库快照；列表后续更新时数量可能变化。'
      : value.basis === 'platform_home'
        ? `保留平台首页数字；${reason}。点击查看已存列表，数量可能不同。`
        : value.basis === 'partial_list'
          ? '仅统计已存列表，尚不能代表全部待办。'
          : value.basis === 'mixed'
            ? `${value.list_shops} 家使用完整列表，${value.platform_shops || 0} 家保留平台首页数字，${value.partial_shops || 0} 家列表未完整；点击查看同筛选下的已存数据。`
            : '尚无可用数字。';
  const listCount =
    value.list_count != null && Number.isFinite(Number(value.list_count))
      ? `已存列表${scope.status || ''} ${Number(value.list_count).toLocaleString('zh-CN')} 条；`
      : '';
  return `${countBasisLabel(value)} · ${time}${range ? ' · 列表范围 ' + range : ''}${scope.status ? ' · ' + scope.status : ''}。${explanation} ${listCount}列表：${listStateLabel(value)}。`;
}

// One short status for cards and table cells; the full basis stays in countBasisDetail (hover).
export function countBasisBrief(value = {}) {
  if (!value.basis || value.basis === 'missing') return '尚未读取';
  if (value.missing) return `${value.missing} 家缺数据`;
  if (value.basis === 'partial_list') return '列表未完整';
  return '';
}
