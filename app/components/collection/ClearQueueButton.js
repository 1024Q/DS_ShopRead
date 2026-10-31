'use client';
import { useState } from 'react';
import { Alert, Button, Modal, Table } from 'antd';
import { settingsRequest } from '../../lib/module-save.mjs';
import { scopeLabel } from '../../lib/node-policy.mjs';

export default function ClearQueueButton() {
  const [modal, contextHolder] = Modal.useModal();
  const [busy, setBusy] = useState(false),
    [message, setMessage] = useState(''),
    [results, setResults] = useState([]);
  async function clear() {
    if (busy) return;
    setBusy(true);
    setMessage('正在确认机器范围…');
    setResults([]);
    try {
      const scope = await settingsRequest();
      const confirmed = await modal.confirm({
        title: `清空${scopeLabel(scope)}的本轮等待需求？`,
        content: '正在执行的任务、已入库数据和分页断点保留。被清理的步骤在下次到期后继续；不会开启停用任务。',
        okText: '清空本轮等待',
        cancelText: '取消',
      });
      if (!confirmed) {
        setMessage('');
        return;
      }
      setMessage('正在清空本轮等待…');
      const result = await settingsRequest('POST', {}, 20000, scope.scope_node_id, 'system/clear-queue');
      setResults(result.results || []);
      setMessage(`已清空 ${result.count || 0} 项等待需求。正在运行的任务继续执行。`);
    } catch (error) {
      setMessage(error.message);
      setResults(error.results || []);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div>
      {contextHolder}
      <Button loading={busy} onClick={clear}>
        清空等待
      </Button>
      {message && <Alert style={{ marginTop: 12 }} role="status" type="info" title={message} />}
      {results.length > 0 && (
        <Table
          size="small"
          rowKey="node"
          pagination={false}
          dataSource={results}
          columns={[
            { title: '节点', dataIndex: 'node' },
            { title: '执行结果', render: (_, r) => (r.ok ? `已清空 ${r.count} 项` : r.reason) },
          ]}
        />
      )}
    </div>
  );
}
