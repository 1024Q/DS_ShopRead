'use client';
import { Alert, Button, Space, Table, Tooltip } from 'antd';
import { useMemo } from 'react';
import useStoredOverview from '../../hooks/useStoredOverview';
import { useWorkspace } from '../../providers/WorkspaceContext';
import { inScope } from '../../lib/filter-scope.mjs';
import { canReadShop } from '../../lib/platforms.mjs';
import { mergeShopOperations, operationsCsv } from '../../lib/shop-operations.mjs';
import { shopLabel } from '../../lib/shop-identity.mjs';
import { overviewTarget } from '../../lib/overview-navigation.mjs';
import UpdatedAgo from '../shared/UpdatedAgo';

const numeric = (value) => value != null && value !== '' && Number.isFinite(Number(value));
const number = (value) => (numeric(value) ? Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '—');
const money = (value) =>
  numeric(value)
    ? '¥' + Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : '—';
const percent = (value) => (numeric(value) ? number(value) + '%' : '—');
const businessFields = [
  ['sales', '店铺成交金额', money],
  ['orders', '今日订单', number],
  ['refund_orders', '退款订单数', number],
  ['refund_amount', '退款金额', money],
  ['ad_spend', '推广花费', money],
  ['visitors', '店铺访客数', number],
  ['inquiry_conversion', '询单转化率', percent],
  ['pending_shipping', '待发货', number],
  ['pending_aftersales', '售后', number],
  ['pending_tickets', '工单', number],
];
const goodsFields = [
  ['visitors', '商品访客数', number],
  ['views', '商品浏览量', number],
  ['sales', '商品成交金额', money],
  ['orders', '商品成交订单数', number],
  ['buyers', '商品成交买家数', number],
  ['conversion', '商品转化率', percent],
];
const compare = (get) => (a, b) => {
  const left = get(a),
    right = get(b);
  return (numeric(left) ? Number(left) : -Infinity) - (numeric(right) ? Number(right) : -Infinity) || 0;
};

export default function ShopOperations({ data, shops = [], shopId, platform, onSelectShop, onNavigate, onOpen }) {
  const { businessDay } = useWorkspace();
  const scope = useMemo(
    () => shops.filter((s) => inScope(shopId, s.id) && inScope(platform, s.platform || 'pdd')),
    [shops, shopId, platform],
  );
  const snapshot = useStoredOverview(shopId, platform);
  const rows = useMemo(
    () =>
      mergeShopOperations(
        scope,
        data,
        { metrics: snapshot.data?.rows, sources: snapshot.data?.rows },
        snapshot.data?.sections?.trades,
        snapshot.data?.sections?.ads,
        snapshot.data?.sections?.refunds,
        businessDay,
      ),
    [scope, data, snapshot.data, businessDay],
  );
  const openCount = (field, row) => {
    const target = overviewTarget(field, { shop_id: String(row.shop_id), platform: row.platform });
    if (target) onNavigate?.(target.kind, target.filters);
  };
  const columns = [
    {
      title: '店铺',
      key: 'shop',
      fixed: 'left',
      width: 230,
      sorter: (a, b) => shopLabel(a).localeCompare(shopLabel(b), 'zh-CN'),
      render: (_, row) => (
        <>
          <Button type="link" disabled={!row.goods_available} onClick={() => onSelectShop(String(row.shop_id))}>
            {shopLabel(row)}
          </Button>
          <div>
            <UpdatedAgo value={row.updated_at} />
            {row.error || row.stale ? ' · 上次数据' : ''}
          </div>
        </>
      ),
    },
    {
      title: `店铺经营 · 今日 ${businessDay}`,
      children: businessFields.map(([key, title, format]) => ({
        title,
        key: 'home-' + key,
        width: 145,
        sorter: compare((r) => r[key]),
        render: (_, row) =>
          key.startsWith('pending_') && onNavigate ? (
            <Button type="link" onClick={() => openCount(key, row)}>
              {format(row[key])}
            </Button>
          ) : (
            format(row[key])
          ),
      })),
    },
    {
      title: '商品概况 · 所选时间范围',
      children: goodsFields.map(([key, title, format]) => ({
        title,
        key: 'goods-' + key,
        width: 155,
        sorter: compare((r) => r.goods_summary[key]),
        render: (_, row) => format(row.goods_summary[key]),
      })),
    },
    { title: '平台更新时间', key: 'source', width: 175, render: (_, row) => row.source_time || '未提供' },
    { title: '商品统计时间', key: 'goods-source', width: 175, render: (_, row) => row.goods_source_time || '未提供' },
    {
      title: '操作',
      key: 'actions',
      width: 110,
      render: (_, row) => (
        <Button type="link" onClick={() => onOpen(row.shop_id, canReadShop(row) ? 'overview' : 'home')}>
          查看后台
        </Button>
      ),
    },
  ];
  function download() {
    const headers = [
      '店铺',
      ...businessFields.map(([, title]) => '今日-' + title),
      ...goodsFields.map(([, title]) => '所选范围-' + title),
      '退款统计日',
      '平台更新时间',
      '商品统计时间',
    ];
    const values = rows.map((row) => [
      shopLabel(row),
      ...businessFields.map(([key]) => row[key]),
      ...goodsFields.map(([key]) => row.goods_summary[key]),
      row.date,
      row.source_time,
      row.goods_source_time,
    ]);
    const url = URL.createObjectURL(new Blob([operationsCsv(headers, values)], { type: 'text/csv;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `店铺经营与商品概况-${businessDay}.csv`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return (
    <section aria-label="店铺经营与商品概况">
      <Space style={{ width: '100%', justifyContent: 'space-between', margin: '16px 0' }} wrap>
        <h3>
          店铺经营与商品概况 <small>{rows.length} 家</small>
        </h3>
        <Tooltip title="导出当前所选店铺的全部经营与商品概况数据">
          <Button onClick={download} disabled={!rows.length}>
            导出合并明细
          </Button>
        </Tooltip>
      </Space>
      <p className="data-note">
        店铺经营列固定为今日平台首页口径；商品概况按上方所选时间范围统计，两者不相加、不互相填补。退款为今日下单且退款成功的订单；缺失显示“—”。
      </p>
      {snapshot.error && <Alert type="warning" showIcon title="部分店铺经营数据暂不可用，商品数据继续展示" />}
      <Table
        rowKey="shop_id"
        columns={columns}
        dataSource={rows}
        scroll={{ x: 3000 }}
        pagination={{ defaultPageSize: 20, showSizeChanger: true, showTotal: (n) => `共 ${n} 家店铺` }}
      />
    </section>
  );
}
