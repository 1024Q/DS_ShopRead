'use client';
import { useEffect, useState } from 'react';
import { Alert, Button, Card, Form, Input, Spin, Typography } from 'antd';
import { fetchJson } from '../../lib/fetch-json';
export default function LoginPanel({ onReady }) {
  const [state, setState] = useState(null),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    fetchJson('/api/auth/session')
      .then((value) => {
        setState(value);
        if (value.user) onReady?.(value.user);
      })
      .catch((e) => setError(e.message));
  }, [onReady]);
  const setup = state && !state.initialized;
  return (
    <div className="account-login">
      <Card title={setup ? '设置管理员账号' : '登录多店铺数据工作台'} style={{ width: 420, maxWidth: '100%' }}>
        {!state && !error ? (
          <Spin />
        ) : (
          <>
            {setup && (
              <Typography.Paragraph type="secondary">
                首次使用，请在本机设置管理员账号。完成后可在“账号与权限”中新增账号并分配页面权限。
              </Typography.Paragraph>
            )}
            {setup && !state.setupAllowed ? (
              <Alert type="warning" title="请先在服务器本机打开后台，完成管理员初始化" />
            ) : (
              <Form
                layout="vertical"
                onFinish={async (values) => {
                  setBusy(true);
                  setError('');
                  try {
                    const result = await fetchJson('/api/auth/' + (setup ? 'setup' : 'login'), {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify(values),
                    });
                    location.replace(result.next);
                  } catch (e) {
                    setError(e.message);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                <Form.Item
                  label="账号"
                  name="username"
                  rules={[
                    { required: true, message: '请输入账号' },
                    { pattern: /^[a-zA-Z0-9_.@-]{3,64}$/, message: '3至64位字母、数字或 _.@-' },
                  ]}
                >
                  <Input autoComplete="username" maxLength={64} />
                </Form.Item>
                <Form.Item
                  label="密码"
                  name="password"
                  rules={[
                    { required: true, message: '请输入密码' },
                    ...(setup ? [{ min: 12, message: '密码至少12个字符' }] : []),
                  ]}
                >
                  <Input.Password autoComplete={setup ? 'new-password' : 'current-password'} maxLength={256} />
                </Form.Item>
                {setup && (
                  <Form.Item
                    label="确认密码"
                    name="confirm"
                    dependencies={['password']}
                    rules={[
                      { required: true, message: '请再次输入密码' },
                      ({ getFieldValue }) => ({
                        validator: (_, v) =>
                          v === getFieldValue('password') ? Promise.resolve() : Promise.reject(Error('两次密码不一致')),
                      }),
                    ]}
                  >
                    <Input.Password autoComplete="new-password" />
                  </Form.Item>
                )}
                {error && <Alert type="error" title={error} style={{ marginBottom: 16 }} />}
                <Button type="primary" htmlType="submit" block loading={busy}>
                  {setup ? '创建管理员并进入后台' : '登录'}
                </Button>
              </Form>
            )}
          </>
        )}
      </Card>
    </div>
  );
}
