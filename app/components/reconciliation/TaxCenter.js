'use client';
import { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Table,
  Tag,
} from 'antd';
const empty = {
  rate: null,
  base: 'net_receipts',
  actual: null,
  included: '0',
  included_in: 'revenue',
  reference: '',
  evidence: '',
};
const monthNow = () => {
  const d = new Date();
  d.setDate(1);
  d.setMonth(d.getMonth() - 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
};
async function api(path, body) {
  const r = await fetch('/api/profit-tax' + path, {
    method: body ? 'PUT' : 'GET',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(60000),
  });
  const d = await r.json();
  if (!r.ok) throw Error(d.detail || '税费设置读取失败');
  return d;
}
export default function TaxCenter() {
  const [month, setMonth] = useState(monthNow),
    [shop, setShop] = useState(0),
    [data, setData] = useState({ items: [], shops: [], history: [] }),
    [config, setConfig] = useState(empty),
    [revision, setRevision] = useState(0),
    [dirty, setDirty] = useState(false),
    [apply, setApply] = useState(false),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(true),
    [error, setError] = useState(''),
    [notice, setNotice] = useState(''),
    [refresh, setRefresh] = useState(0),
    [modal, context] = Modal.useModal();
  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError('');
    api('?' + new URLSearchParams({ month }))
      .then((d) => {
        if (alive) {
          setData(d);
          const row = d.items.find((i) => i.shop_id === shop);
          const fallback = d.items.find((i) => i.shop_id === 0);
          setConfig(
            row?.config || { ...empty, rate: fallback?.config.rate ?? null, base: fallback?.config.base || empty.base },
          );
          setRevision(row?.revision || 0);
          setDirty(false);
          setApply(false);
        }
      })
      .catch((e) => {
        if (alive) setError(e.message);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [month, shop, refresh]);
  const change = (k, v) => {
    setConfig((c) => ({ ...c, [k]: v }));
    setDirty(true);
    setNotice('');
  };
  async function save() {
    if (
      apply &&
      !(await modal.confirm({
        title: '更新该月份已有报表的税费版本？',
        content: shop
          ? '此店铺所选月份报表将重新计算税费；已核对利润会要求重新核对。'
          : '仅更新所选月份使用统一默认的店铺，保留已设店铺覆盖的报表。原版本保留可查。',
        okText: '确认重新核算',
        cancelText: '取消',
      }))
    )
      return;
    setBusy(true);
    setError('');
    try {
      const d = await api(`/${shop}/${month}`, { revision, config, apply_existing: apply });
      setNotice(
        `税费版本已保存，已更新 ${d.affected} 份历史报表。${apply ? '' : '历史报表保持原版本，新导入报表使用新版本。'}`,
      );
      setRefresh((v) => v + 1);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section style={{ padding: 24 }}>
      {context}
      <Space wrap style={{ justifyContent: 'space-between', width: '100%' }}>
        <h1>税费预估与核对</h1>
        <a href="/?view=profit">查看月度税后利润 →</a>
      </Space>
      <Alert
        type="info"
        showIcon
        message="按店铺和月份核算，历史报表绑定税费版本"
        description="自定义税点用于经营测算。实际税费替换预估税费；已在实收或费用中扣除的税额不再重复扣。各月份独立设置，不自动沿用其他月份。"
        style={{ marginBottom: 20 }}
      />
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
      {notice && <Alert type="success" showIcon message={notice} style={{ marginBottom: 16 }} />}
      <Row gutter={[20, 20]}>
        <Col xs={24} xl={12}>
          <Card title="税费设置" loading={loading}>
            <Form layout="vertical" disabled={busy}>
              <Row gutter={16}>
                <Col xs={24} sm={10}>
                  <Form.Item label="核算月份">
                    <Input
                      aria-label="税费月份"
                      type="month"
                      value={month}
                      disabled={dirty || busy}
                      onChange={(e) => {
                        if (e.target.value) setMonth(e.target.value);
                      }}
                    />
                  </Form.Item>
                </Col>
                <Col xs={24} sm={14}>
                  <Form.Item label="适用店铺">
                    <Select
                      aria-label="适用店铺"
                      showSearch
                      optionFilterProp="label"
                      value={shop}
                      disabled={dirty || busy}
                      onChange={setShop}
                      options={[
                        { value: 0, label: '统一默认（所选月份）' },
                        ...data.shops.map((s) => ({ value: s.id, label: s.name })),
                      ]}
                    />
                  </Form.Item>
                </Col>
              </Row>
              <Form.Item label="税点（百分比）" required>
                <InputNumber
                  aria-label="税点"
                  min={0}
                  max={100}
                  precision={6}
                  addonAfter="%"
                  value={config.rate}
                  onChange={(v) => change('rate', v)}
                  style={{ width: '100%' }}
                />
              </Form.Item>
              <Form.Item label="计税基数">
                <Select
                  value={config.base}
                  onChange={(v) => change('base', v)}
                  options={[
                    { value: 'receipts', label: '核算实收金额（加补入收入）' },
                    { value: 'net_receipts', label: '核算实收金额 − 尚未反映的退款（加补入收入）' },
                  ]}
                />
              </Form.Item>
              <Form.Item label="实际税费（元，留空使用预估；0表示实际为零）">
                <InputNumber
                  aria-label="实际税费"
                  min={0}
                  precision={2}
                  value={config.actual}
                  disabled={!shop || busy}
                  onChange={(v) => change('actual', v)}
                  style={{ width: '100%' }}
                />
              </Form.Item>
              <Row gutter={16}>
                <Col xs={24} sm={12}>
                  <Form.Item label="已包含的税费（元）">
                    <InputNumber
                      aria-label="已包含的税费"
                      min={0}
                      precision={2}
                      value={config.included}
                      disabled={!shop || busy}
                      onChange={(v) => change('included', v ?? 0)}
                      style={{ width: '100%' }}
                    />
                  </Form.Item>
                </Col>
                <Col xs={24} sm={12}>
                  <Form.Item label="已扣税费位置">
                    <Select
                      value={config.included_in}
                      disabled={!shop || busy}
                      onChange={(v) => change('included_in', v)}
                      options={[
                        { value: 'revenue', label: '已在实收中扣除' },
                        { value: 'fees', label: '已在费用核对中扣除' },
                      ]}
                    />
                  </Form.Item>
                </Col>
              </Row>
              <Form.Item label="税费凭据编号">
                <Input value={config.reference} maxLength={200} onChange={(e) => change('reference', e.target.value)} />
              </Form.Item>
              <Form.Item label="核对依据">
                <Input.TextArea
                  rows={3}
                  value={config.evidence}
                  maxLength={3000}
                  onChange={(e) => change('evidence', e.target.value)}
                  placeholder="账单、缴税凭据及已扣税额说明。费用核对需使用相同凭据编号。"
                />
              </Form.Item>
              <Form.Item>
                <Checkbox
                  checked={apply}
                  onChange={(e) => {
                    setApply(e.target.checked);
                    setDirty(true);
                  }}
                >
                  同时更新所选月份已有报表并重新核算
                </Checkbox>
              </Form.Item>
              <Space>
                <Button
                  type="primary"
                  loading={busy}
                  disabled={loading || !dirty || config.rate === null}
                  onClick={save}
                >
                  保存税费版本
                </Button>
                <Button disabled={busy || !dirty} onClick={() => setRefresh((v) => v + 1)}>
                  取消修改
                </Button>
              </Space>
            </Form>
          </Card>
        </Col>
        <Col xs={24} xl={12}>
          <Card title="计算与去重规则">
            <p>新增扣税＝实际税费（未填写则使用预估税费）− 已包含税费。</p>
            <p>税后利润＝原经营利润 − 新增扣税。退款已包含在实收中的，仅留档，不再减少计税基数。</p>
            <p>
              若税费已登记为平台费用或其他费用，选择“费用核对”，填写相同凭据编号及已扣税额；不一致时暂停税后核算并提示处理。
            </p>
            <p>
              明细按正实收金额分摊税额，尾差自动平衡。明细税后商品毛利不含推广与整店费用，完整税后利润请查看月度利润页。
            </p>
            <p>未设置税费不按零计算。设定0%可明确表示预估税费为零；已核对税后利润还需实际税费及完整月度核对。</p>
          </Card>
          <Card title="所选月份配置" style={{ marginTop: 20 }}>
            <Table
              size="small"
              rowKey="shop_id"
              scroll={{ x: 520 }}
              pagination={false}
              dataSource={data.items}
              columns={[
                {
                  title: '店铺',
                  render: (_, r) =>
                    r.shop_id === 0 ? '统一默认' : data.shops.find((s) => s.id === r.shop_id)?.name || r.shop_id,
                },
                { title: '税点', render: (_, r) => r.config.rate + '%' },
                { title: '实际税费', render: (_, r) => r.config.actual ?? '预估' },
                { title: '版本', render: (_, r) => <Tag>{r.id.slice(0, 8)}</Tag> },
              ]}
            />
          </Card>
        </Col>
      </Row>
      <Card title="税费配置历史（版本保留）" style={{ marginTop: 20 }}>
        <Table
          size="small"
          rowKey="id"
          scroll={{ x: 900 }}
          dataSource={data.history}
          columns={[
            {
              title: '店铺',
              render: (_, r) =>
                r.shop_id === 0 ? '统一默认' : data.shops.find((s) => s.id === r.shop_id)?.name || r.shop_id,
            },
            { title: '版本', dataIndex: 'id' },
            { title: '保存时间', render: (_, r) => new Date(r.created * 1000).toLocaleString('zh-CN') },
            { title: '税点', render: (_, r) => r.config.rate + '%' },
            { title: '实际税费', render: (_, r) => r.config.actual ?? '—' },
            { title: '已包含税费', render: (_, r) => r.config.included },
            { title: '凭据 / 依据', render: (_, r) => r.config.reference + ' ' + r.config.evidence },
          ]}
        />
      </Card>
    </section>
  );
}
