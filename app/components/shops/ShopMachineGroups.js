'use client';
import { ActionButton } from '../shared/AdminControls';
import { useState, useEffect } from 'react';
import { shopGroupPage } from '../../lib/shop-group-page.mjs';

export default function ShopMachineGroups({ shops, layout, children, onMove }) {
  const [collapsed, setCollapsed] = useState({});
  const [pages, setPages] = useState({});
  const [nodes, setNodes] = useState([]),
    [dragged, setDragged] = useState(null),
    [over, setOver] = useState(null);
  useEffect(() => {
    let alive = true;
    fetch('/api/nodes')
      .then((r) => (r.ok ? r.json() : []))
      .then((n) => {
        if (alive && Array.isArray(n)) setNodes(n);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);
  const groups = new Map();
  for (const node of nodes)
    groups.set(String(node.node), {
      key: String(node.node),
      name: node.name,
      node: node.id,
      online: node.online,
      shops: [],
    });
  for (const shop of shops) {
    const key = String(shop.node_id ?? shop.machine_id ?? 'local');
    if (!groups.has(key)) groups.set(key, { key, name: shop.machine_name || '本机采集节点', shops: [] });
    groups.get(key).name = shop.machine_name || groups.get(key).name;
    groups.get(key).shops.push(shop);
  }
  return [...groups.values()].map((group) => {
    const signature = group.shops.map((s) => s.id).join(',');
    const {
      page,
      pages: pageCount,
      visible,
    } = shopGroupPage(group.shops, pages[group.key]?.signature === signature ? pages[group.key].page : 1);
    const turn = (next) => setPages((old) => ({ ...old, [group.key]: { signature, page: next } }));
    const canDrop = dragged && String(dragged.node_id) !== group.key && group.node && group.online;
    return (
      <section
        className={`shop-machine-group ${over === group.key ? 'shop-drop-active' : ''}`}
        key={group.key}
        aria-label={group.name}
        onDragOver={(e) => {
          if (canDrop) {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            setOver(group.key);
          }
        }}
        onDragLeave={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget)) setOver(null);
        }}
        onDrop={(e) => {
          e.preventDefault();
          if (canDrop) onMove?.(dragged, group.node);
          setDragged(null);
          setOver(null);
        }}
      >
        <div className="shop-machine-heading">
          <h3>{group.name}</h3>
          <span
            className={`node-connection ${(group.online ?? group.shops.some((s) => s.collector_online === true)) ? 'online' : ''}`}
          >
            {(group.online ?? group.shops.some((s) => s.collector_online === true))
              ? '在线'
              : group.online === false || group.shops.every((s) => s.collector_online === false)
                ? '离线'
                : '状态未知'}
          </span>
          <span>
            {group.shops.length} 家店铺 · {group.shops.filter((s) => s.state === 'logged_in').length} 家已登录
          </span>
          <ActionButton
            type="button"
            aria-expanded={!collapsed[group.key]}
            onClick={() => setCollapsed((old) => ({ ...old, [group.key]: !old[group.key] }))}
          >
            {collapsed[group.key] ? '展开' : '收起'}
          </ActionButton>
        </div>
        {!collapsed[group.key] && (
          <>
            <div className={`cards shop-cards ${layout === 'list' ? 'shop-list-mode' : ''}`}>
              {visible.map((shop) => (
                <div
                  key={shop.id}
                  className="shop-drag-card"
                  draggable={!!onMove}
                  onDragStart={(e) => {
                    e.dataTransfer.setData('text/plain', String(shop.id));
                    e.dataTransfer.effectAllowed = 'move';
                    setDragged(shop);
                  }}
                  onDragEnd={() => {
                    setDragged(null);
                    setOver(null);
                  }}
                >
                  {children(shop)}
                </div>
              ))}
              {!visible.length && <p className="form-hint">暂无店铺 · 可将店铺拖到此节点</p>}
            </div>
            {pageCount > 1 && (
              <div className="pagination" aria-label={group.name + '店铺分页'}>
                <span>
                  第 {page} / {pageCount} 页 · {group.shops.length} 家
                </span>
                <ActionButton disabled={page <= 1} onClick={() => turn(page - 1)}>
                  上一页
                </ActionButton>
                <ActionButton disabled={page >= pageCount} onClick={() => turn(page + 1)}>
                  下一页
                </ActionButton>
              </div>
            )}
          </>
        )}
      </section>
    );
  });
}
