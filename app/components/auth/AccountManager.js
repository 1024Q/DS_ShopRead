'use client';
import { useEffect, useState } from 'react';
import { Alert, Button, Form, Input, Modal, Select, Space, Switch, Table } from 'antd';
import { fetchJson } from '../../lib/fetch-json';
export default function AccountManager() {
  const [rows, setRows] = useState([]),
    [pages, setPages] = useState({}),
    [editing, setEditing] = useState(null),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false);
  const [form] = Form.useForm();
  const role = Form.useWatch('role', form);
  async function load() {
    try {
      const [list, session] = await Promise.all([fetchJson('/api/auth/users'), fetchJson('/api/auth/session')]);
      setRows(list);
      setPages(session.pages);
    } catch (e) {
      setError(e.message);
    }
  }
  useEffect(() => {
    load();
  }, []);
  function edit(user) {
    setEditing(user);
    form.resetFields();
    form.setFieldsValue({ role: 'user', ...user });
  }
  return (
    <section className="data-panel account-manager">
      <h2>账号与权限</h2>
      <p>每个页面可设置隐藏、只读或允许操作。修改权限、停用账号或重置密码后，原登录会话立即失效。</p>
      {error && <Alert type="error" title={error} closable onClose={() => setError('')} />}
      <Button type="primary" onClick={() => edit({})}>
        新增账号
      </Button>
      <Table
        rowKey="id"
        dataSource={rows}
        pagination={false}
        scroll={{ x: 600 }}
        columns={[
          { title: '账号', dataIndex: 'username' },
          { title: '角色', render: (_, r) => (r.role === 'admin' ? '管理员' : '普通账号') },
          { title: '状态', render: (_, r) => (r.disabled ? '已停用' : '启用') },
          {
            title: '页面权限',
            render: (_, r) => (r.role === 'admin' ? '全部页面' : `${Object.keys(r.permissions).length} 个页面`),
          },
          { title: '操作', render: (_, r) => <Button onClick={() => edit(r)}>编辑 / 重置密码</Button> },
        ]}
      />
      <Modal
        title={editing?.id ? '编辑账号' : '新增账号'}
        open={!!editing}
        onCancel={() => !busy && setEditing(null)}
        onOk={() => form.submit()}
        confirmLoading={busy}
        width={680}
        style={{ top: 16 }}
        styles={{ body: { maxHeight: 'calc(100dvh - 180px)', overflowY: 'auto', paddingRight: 4 } }}
        destroyOnHidden
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={async (values) => {
            setBusy(true);
            setError('');
            const permissions = Object.fromEntries(
              Object.entries(values.permissions || {}).filter(([, v]) => v === 'read' || v === 'write'),
            );
            try {
              await fetchJson('/api/auth/users' + (editing.id ? '/' + editing.id : ''), {
                method: editing.id ? 'PUT' : 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ...values, permissions }),
              });
              setEditing(null);
              await load();
            } catch (e) {
              setError(e.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <Form.Item label="账号" name="username" rules={[{ required: true }]}>
            <Input autoComplete="off" maxLength={64} />
          </Form.Item>
          <Form.Item
            label={editing?.id ? '新密码（不修改请留空）' : '密码'}
            name="password"
            rules={[{ required: !editing?.id }, { min: 12, message: '密码至少12个字符' }]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Space align="start">
            <Form.Item label="角色" name="role">
              <Select
                style={{ width: 160 }}
                options={[
                  { value: 'user', label: '普通账号' },
                  { value: 'admin', label: '管理员' },
                ]}
              />
            </Form.Item>
            <Form.Item label="停用账号" name="disabled" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>
          {role !== 'admin' && (
            <div className="account-permissions">
              {Object.entries(pages).map(([key, label]) => (
                <Form.Item key={key} label={label} name={['permissions', key]}>
                  <Select
                    allowClear
                    placeholder="隐藏 / 无权限"
                    options={
                      key === 'machines'
                        ? [{ value: 'read', label: '只读（管理操作仅管理员）' }]
                        : [
                            { value: 'read', label: '只读' },
                            { value: 'write', label: '查看和操作' },
                          ]
                    }
                  />
                </Form.Item>
              ))}
            </div>
          )}
          {error && <Alert type="error" title={error} />}
        </Form>
      </Modal>
    </section>
  );
}
