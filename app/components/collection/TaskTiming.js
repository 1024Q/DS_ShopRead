'use client';
import { Tag } from 'antd';

const phases = [
  ['slot_wait', '等名额'],
  ['browser_start', '启动浏览器'],
  ['page_load', '加载页面'],
  ['query', '查询接口'],
  ['database', '入库'],
];
export default function TaskTiming({ step }) {
  const current = step.timing_current;
  const live = Object.values(step.timing_seconds || {}).some((value) => value > 0);
  const values = live ? step.timing_seconds : step.last_timing?.timing_seconds;
  const upload = step.upload_timing;
  if (!values && !current && !upload) return <small>尚无耗时记录</small>;
  return (
    <div style={{ maxWidth: 620, whiteSpace: 'normal', lineHeight: 2 }}>
      {current && (
        <div>
          <Tag color="blue">{current.reason}</Tag>已持续 {current.elapsed_seconds ?? 0} 秒
        </div>
      )}
      <small>
        {step.timing_basis || (live ? '本次' : '上次执行')}：
        {phases.map(([key, name]) => `${name} ${Number(values?.[key] || 0).toFixed(1)}秒`).join(' · ')}
      </small>
      {upload && (
        <div>
          <Tag color={upload.state === 'confirmed' ? 'green' : upload.state === 'failed' ? 'red' : 'orange'}>
            {upload.state === 'confirmed'
              ? '中心已确认'
              : upload.state === 'failed'
                ? '上传失败，待重试'
                : '等待上传确认'}
          </Tag>
          {upload.seconds} 秒{upload.reason ? ` · ${upload.reason}` : ''}
        </div>
      )}
      {step.reuse_counts && (
        <small>
          接口请求 {step.reuse_counts.network_queries || 0} · 缓存命中 {step.reuse_counts.cache_hits || 0} · 合并等待{' '}
          {step.reuse_counts.joined_queries || 0}
        </small>
      )}
    </div>
  );
}
