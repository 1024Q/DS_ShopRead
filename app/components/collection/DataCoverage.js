'use client';
import { freshness } from '../../lib/freshness.mjs';
export default function DataCoverage({ sources = [] }) {
  if (!sources.length) return null;
  const complete = sources.filter((s) => s.complete && s.updated_at).length,
    missing = sources.filter((s) => !s.updated_at).length;
  const dates = sources
    .map((s) => s.updated_at)
    .filter(Boolean)
    .sort();
  if (sources.some((source) => source.details_on_demand)) {
    const summaries = sources.filter(
      (source) => (source.details_on_demand ? source.summary_complete : source.complete) && source.updated_at,
    ).length;
    const details = sources.filter(
      (source) =>
        (source.details_on_demand ? source.details_complete : source.complete) &&
        (source.details_updated_at || source.updated_at),
    ).length;
    return (
      <div className="data-coverage data-note">
        汇总已读取 {summaries} / {sources.length} 家 · 完整明细 {details} / {sources.length} 家 · 最早汇总：
        {freshness({ updated_at: dates[0], stale: sources.some((source) => source.stale) }).label}
      </div>
    );
  }
  return (
    <div className="data-coverage data-note">
      数据完整度 {complete} / {sources.length} 家 · 缺失 {missing} 家 · 最早数据：
      {freshness({ updated_at: dates[0], stale: sources.some((s) => s.stale) }).label}
    </div>
  );
}
