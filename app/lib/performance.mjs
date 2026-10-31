export function compareTrend(trend, metric, today, yesterday) {
  const valid = (v) => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
  const series = ['今日累计', '昨日累计'].map((name, index) =>
    [
      ...new Map(
        (trend || [])
          .filter(
            (r) =>
              r.series === name &&
              r.date === (index ? yesterday : today) &&
              valid(r.hour) &&
              Number(r.hour) >= 0 &&
              Number(r.hour) <= 23 &&
              valid(r[metric]),
          )
          .map((r) => [Number(r.hour), { hour: Number(r.hour), value: Number(r[metric]) }]),
      ).values(),
    ].sort((a, b) => a.hour - b.hour),
  );
  const common = series[0].filter((r) => series[1].some((v) => v.hour === r.hour));
  const current = common.at(-1),
    previous = series[1].find((r) => r.hour === current?.hour);
  return {
    series,
    hour: current?.hour ?? null,
    today: current?.value ?? null,
    yesterday: previous?.value ?? null,
    change:
      previous?.value > 0
        ? ((current.value - previous.value) / previous.value) * 100
        : previous?.value === 0 && current?.value === 0
          ? 0
          : null,
  };
}

export function aggregateTrend(metrics, metric, today, yesterday) {
  const records = (metrics || [])
    .map((m) => compareTrend(m.trend, metric, today, yesterday))
    .filter((c) => c.series.some((s) => s.length));
  const rows = [];
  if (!records.length) return { rows, count: 0 };
  for (let series = 0; series < 2; series++)
    for (let hour = 0; hour < 24; hour++) {
      const points = records
        .map((r, index) => {
          const point = r.series[series].find((p) => p.hour === hour);
          return point ? { ...point, shop: index } : null;
        })
        .filter(Boolean);
      if (points.length)
        rows.push({
          id: `${series}:${hour}`,
          series: series ? '昨日累计' : '今日累计',
          date: series ? yesterday : today,
          hour,
          coverage: points.length,
          cohort: points.map((p) => p.shop).join(','),
          [metric]: points.reduce((v, p) => v + p.value, 0),
        });
    }
  return { rows, count: records.length };
}
