'use client';
import { useState } from 'react';
import { Alert, Button, Form, Input, Modal, Space } from 'antd';
import { fetchJson } from '../../lib/fetch-json';
export default function SessionMenu({ user }) {
  const [open, setOpen] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  return (
    <>
      <Space wrap size="small" className="session-menu">
        <span>{user?.username}</span>
        {user?.role === 'admin' && (
          <a href="/?view=accounts" style={{ whiteSpace: 'nowrap' }}>
            账号与权限
          </a>
        )}
        <Button onClick={() => setOpen(true)}>修改密码</Button>
        <Button
          onClick={async () => {
            await fetchJson('/api/auth/logout', { method: 'POST' });
            location.replace('/center');
          }}
        >
          退出
        </Button>
      </Space>
      <Modal title="修改密码" open={open} onCancel={() => !busy && setOpen(false)} footer={null} destroyOnHidden>
        <Form
          layout="vertical"
          onFinish={async (values) => {
            setBusy(true);
            setError('');
            try {
              await fetchJson('/api/auth/password', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(values),
              });
              location.replace('/center');
            } catch (e) {
              setError(e.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <Form.Item label="当前密码" name="currentPassword" rules={[{ required: true }]}>
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Form.Item
            label="新密码"
            name="password"
            rules={[{ required: true }, { min: 12, message: '密码至少12个字符' }]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          {error && <Alert type="error" title={error} />}
          <Button type="primary" htmlType="submit" loading={busy}>
            保存并重新登录
          </Button>
        </Form>
      </Modal>
    </>
  );
}
