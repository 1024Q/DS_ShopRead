'use client';
import { Button, Dropdown, Space, Typography } from 'antd';
import { DownOutlined } from '@ant-design/icons';
export default function ReportTaskActions({ job, busy, onRetry, onDelete, onImport }) {
  const ready = job.state === 'completed',
    failed = job.state === 'exception';
  const items = [
    ...(ready
      ? [{ key: 'download', label: <a href={'/api/report-exports/' + job.id + '/download'}>下载报表</a> }]
      : []),
    ...(job.screenshot
      ? [
          {
            key: 'screenshot',
            label: (
              <a href={'/api/report-exports/' + job.id + '/screenshot'} target="_blank" rel="noreferrer">
                {ready ? '历史失败截图（已恢复）' : '查看失败截图'}
              </a>
            ),
          },
        ]
      : []),
    ...(ready && job.import_state === 'error'
      ? [{ key: 'import', label: '重新导入', onClick: () => onImport(job.id), disabled: busy }]
      : []),
    ...(failed
      ? [{ key: 'retry', label: '确认未生成，重新提交', onClick: () => onRetry(job.id, true), disabled: busy }]
      : []),
    { type: 'divider' },
    { key: 'delete', label: '删除报表', danger: true, disabled: busy, onClick: () => onDelete([job]) },
  ];
  return (
    <Space size={4}>
      {ready && job.import_state === 'ready' ? (
        <Button
          type="link"
          href={'/?view=report_view&report_month=' + job.start.slice(0, 7) + '&report_shop=' + job.shop_id}
        >
          查看报表
        </Button>
      ) : failed ? (
        <Button disabled={busy} onClick={() => onRetry(job.id)}>
          恢复 / 重试
        </Button>
      ) : (
        <Typography.Text type="secondary">{ready ? '正在导入' : '自动处理中'}</Typography.Text>
      )}
      <Dropdown trigger={['click']} menu={{ items }}>
        <Button type="text" aria-label={job.shop_name + '报表更多操作'}>
          更多 <DownOutlined />
        </Button>
      </Dropdown>
    </Space>
  );
}
