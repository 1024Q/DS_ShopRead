'use client';
import { useState } from 'react';
import { Input, Select, Checkbox, Button, Empty, Tag } from 'antd';
import { SearchOutlined } from '@ant-design/icons';
export default function ReportShopPicker({ shops, selected, onChange, disabled }) {
  const [search, setSearch] = useState(''),
    [node, setNode] = useState('');
  const names = [...new Set(shops.map((s) => s.node_name || '未命名节点'))];
  const visible = shops.filter(
    (s) =>
      (!node || (s.node_name || '未命名节点') === node) &&
      (!search || `${s.name} ${s.node_name}`.toLowerCase().includes(search.trim().toLowerCase())),
  );
  const picked = new Set(selected),
    online = visible.filter((s) => s.online),
    full = online.length > 0 && online.every((s) => picked.has(s.id));
  function toggle(items, checked) {
    const ids = new Set(items.filter((s) => s.online).map((s) => s.id));
    onChange(checked ? [...new Set([...selected, ...ids])].slice(0, 50) : selected.filter((id) => !ids.has(id)));
  }
  return (
    <div className="admin-shop-picker">
      <div className="admin-picker-search">
        <Input
          allowClear
          prefix={<SearchOutlined />}
          aria-label="搜索导出店铺"
          placeholder="搜索店铺名称"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <Select
          aria-label="筛选采集节点"
          value={node}
          onChange={setNode}
          options={[{ value: '', label: '全部节点' }, ...names.map((n) => ({ value: n, label: n }))]}
        />
      </div>
      <div className="admin-picker-tools">
        <Checkbox
          checked={full}
          indeterminate={!full && online.some((s) => picked.has(s.id))}
          disabled={disabled || !online.length}
          onChange={(e) => toggle(online, e.target.checked)}
        >
          全选当前结果
        </Checkbox>
        <Button type="text" size="small" disabled={disabled || !selected.length} onClick={() => onChange([])}>
          清空
        </Button>
        <Tag>{selected.length} / 50 家</Tag>
      </div>
      <div className="admin-picker-list" aria-label="选择导出店铺">
        {names.map((n) => {
          const rows = visible.filter((s) => (s.node_name || '未命名节点') === n);
          if (!rows.length) return null;
          const available = rows.filter((s) => s.online),
            count = rows.filter((s) => picked.has(s.id)).length;
          return (
            <div key={n}>
              <div className="admin-picker-group">
                <Checkbox
                  checked={available.length > 0 && available.every((s) => picked.has(s.id))}
                  indeterminate={count > 0 && !available.every((s) => picked.has(s.id))}
                  disabled={disabled || !available.length}
                  onChange={(e) => toggle(rows, e.target.checked)}
                >
                  {n}
                </Checkbox>
                <small>
                  {count} / {rows.length}
                </small>
              </div>
              {rows.map((s) => (
                <div key={s.id} className={'admin-picker-row' + (picked.has(s.id) ? ' selected' : '')}>
                  <Checkbox
                    checked={picked.has(s.id)}
                    disabled={disabled || !s.online || (!picked.has(s.id) && selected.length >= 50)}
                    onChange={(e) => toggle([s], e.target.checked)}
                  >
                    {s.name}
                  </Checkbox>
                  <Tag bordered={false} color={s.online ? 'success' : 'default'}>
                    {s.online ? '在线' : '离线'}
                  </Tag>
                </div>
              ))}
            </div>
          );
        })}
        {!visible.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的店铺" />}
      </div>
      <p className="context-note">可按节点批量勾选，离线店铺暂不可选。</p>
    </div>
  );
}
