'use client';
import { Drawer } from 'antd';
import { ActionButton } from '../shared/AdminControls';
import { databaseReader } from '../../lib/database-reader';
import { shopLabel } from '../../lib/shop-identity.mjs';
import { useEffect, useState } from 'react';
import ProductImage from '../shared/ProductImage';
import { beijing, moduleNames } from '../shell/WorkspaceTools';
function scopeText(kind, scope) {
  if (kind === 'products') return '全部商品';
  if (scope?.trade_range)
    return `${scope.day || ''} · ${{ realtime: '今日实时', yesterday: '昨日', week: '近 7 日（截至昨日）', month: '近 30 日（截至昨日）' }[scope.trade_range] || scope.trade_range}`;
  if (scope?.date_from)
    return `${scope.date_from} 至 ${scope.date_to}${scope.status ? ' · ' + scope.status : ''}${scope.stars?.length ? ' · ' + scope.stars.join('、') + ' 星' : ''}${scope.reply ? ' · ' + (scope.reply === 'unreplied' ? '未回复' : '已回复') : ''}`;
  return '历史缓存未记录范围，请刷新关联数据';
}
export default function ProductOverview({ onNavigate }) {
  const [item, setItem] = useState(null),
    [data, setData] = useState(null),
    [error, setError] = useState(''),
    [version, setVersion] = useState(0),
    [loading, setLoading] = useState(false);
  useEffect(() => {
    const open = (e) => {
      setData(null);
      setError('');
      setVersion(0);
      setItem(e.detail);
    };
    window.addEventListener('product-overview', open);
    return () => window.removeEventListener('product-overview', open);
  }, []);
  useEffect(() => {
    if (!item) return;
    let active = true,
      timer,
      attempts = 0,
      busy = false;
    const read = databaseReader(`/api/product/${item.shop_id}/${encodeURIComponent(item.goods_id)}`);
    const c = new AbortController();
    async function load(refresh = false) {
      if (busy) return;
      busy = true;
      setLoading(true);
      try {
        const r = await read(new URLSearchParams(), {
          force: refresh,
          signal: c.signal,
          onPreview: (value) => {
            if (active) setData(value);
          },
        });
        const d = await r.json();
        if (!r.ok) throw Error(d.detail || '详情读取失败');
        if (active) {
          setData(d);
          setError('');
          if (d.sources.some((s) => s.syncing) && attempts++ < 12)
            timer = setTimeout(() => load(), Math.min(3000 * 2 ** Math.floor(attempts / 3), 30000));
        }
      } catch (e) {
        if (active && e.name !== 'AbortError') setError(e.message);
      } finally {
        busy = false;
        if (active) setLoading(false);
      }
    }
    const updated = (e) => {
      if (
        !document.hidden &&
        (e.detail?.reset || e.detail?.changes?.some((c) => String(c.shop_id) === String(item.shop_id)))
      ) {
        clearTimeout(timer);
        load();
      }
    };
    window.addEventListener('database-updated', updated);
    load(version > 0);
    return () => {
      active = false;
      c.abort();
      clearTimeout(timer);
      window.removeEventListener('database-updated', updated);
    };
  }, [item, version]);
  useEffect(() => {
    const close = (e) => {
      if (e.key === 'Escape') setItem(null);
    };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, []);
  if (!item) return null;
  const product = data?.sections.products.rows[0] || {},
    metrics = data?.sections.goodsdata.rows[0];
  return (
    <Drawer open title="商品综合详情" size="large" onClose={() => setItem(null)} className="product-detail-drawer">
      <section className="product-overview">
        <div className="goods-with-thumbnail">
          <ProductImage url={product.thumbnail_url || item.thumbnail_url} name={product.name || item.name} />
          <div>
            <h3>{product.name || item.name}</h3>
            <p>
              {shopLabel(data || item)} · 商品 {item.goods_id}
            </p>
          </div>
        </div>
        <p className="data-note">按本店商品 ID 关联最近记录；未匹配不代表没有相关业务。</p>
        <ActionButton
          disabled={loading || data?.sources.some((s) => s.syncing)}
          onClick={() => setVersion((v) => v + 1)}
        >
          刷新关联数据
        </ActionButton>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="metric-grid">
          {[
            ['库存', product.stock],
            ['累计销量', product.sales],
            ['30 日销量', product.sales_30d],
            ['所选范围成交金额', metrics?.sales],
            ['所选范围访客数', metrics?.visitors],
            ['所选范围成交件数', metrics?.quantity],
          ].map(([label, value]) => (
            <div className="metric-card" key={label}>
              <span>{label}</span>
              <strong>{value ?? '—'}</strong>
            </div>
          ))}
        </div>
        <div className="product-sections">
          {Object.entries(data?.sections || {}).map(([kind, section]) => (
            <section key={kind}>
              <div className="section-head">
                <h3>
                  {moduleNames[kind]} · {section.rows.length} 条关联记录
                </h3>
                <ActionButton
                  className="table-action"
                  onClick={() => {
                    onNavigate(kind, {
                      shop_id: String(item.shop_id),
                      keyword: String(item.goods_id),
                      ...(section.scope?.trade_range ? { trade_range: section.scope.trade_range } : {}),
                      ...(section.scope?.date_from
                        ? { date_from: section.scope.date_from, date_to: section.scope.date_to }
                        : {}),
                      ...(section.scope?.status ? { status: section.scope.status } : {}),
                      ...(section.scope?.stars ? { stars: section.scope.stars } : {}),
                      ...(section.scope?.reply ? { reply: section.scope.reply } : {}),
                    });
                    setItem(null);
                  }}
                >
                  进入模块
                </ActionButton>
              </div>
              <p className="data-note">
                系统读取：{beijing(section.updated_at)} ·{' '}
                {section.updated_at ? (section.complete ? '本次查询已读完整' : '本次查询仅部分数据') : '尚无缓存'}
                <br />
                查询范围：{scopeText(kind, section.scope)}
              </p>
              {section.rows.slice(0, 20).map((r) => (
                <div className="product-related-row" key={r.id}>
                  <strong>{r.status || r.problem || r.name || r.id}</strong>
                  <span>{r.content || r.progress || r.problem || r.remark || ''}</span>
                  {kind === 'quality' && (
                    <small>
                      严重负向反馈 {r.negative_count ?? '—'} 条 · 差评 {r.bad_count ?? '—'} 条 · 差评率{' '}
                      {r.bad_rate == null ? '—' : r.bad_rate + '%'}
                    </small>
                  )}
                  {r.reply && <small>商家回复：{r.reply}</small>}
                  {r.suggestion && <small>整改建议：{r.suggestion}</small>}
                  <small>
                    {r.order_number ? `订单 ${r.order_number} · ` : ''}
                    {r.stars != null ? `${r.stars} 星 · ` : ''}
                    {r.created_at || ''}
                  </small>
                </div>
              ))}
              {section.rows.length > 20 && <p>展示前 20 条，进入模块可查看其余记录。</p>}
              {!section.rows.length && <p>此查询结果未匹配到该商品记录。</p>}
            </section>
          ))}
        </div>
      </section>
    </Drawer>
  );
}
