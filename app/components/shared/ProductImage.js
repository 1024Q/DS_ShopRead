'use client';
import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
export default function ProductImage({ url, name = '' }) {
  const [failed, setFailed] = useState(null),
    [attempt, setAttempt] = useState(0),
    [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const close = (e) => {
      if (e.key === 'Escape') {
        e.stopImmediatePropagation();
        setOpen(false);
      }
    };
    window.addEventListener('keydown', close, true);
    return () => window.removeEventListener('keydown', close, true);
  }, [open]);
  const valid = url && /^https?:\/\//i.test(url);
  return (
    <>
      {valid && failed !== url ? (
        <button
          className="thumbnail-button"
          aria-label={`预览${name || '商品'}主图`}
          onClick={(e) => {
            e.stopPropagation();
            setOpen(true);
          }}
        >
          <img
            key={url + attempt}
            className="goods-thumbnail"
            src={url}
            alt={`${name || '商品'}主图`}
            width="64"
            height="64"
            loading="lazy"
            decoding="async"
            referrerPolicy="no-referrer"
            onError={() => setFailed(url)}
          />
        </button>
      ) : (
        <span className="goods-thumbnail goods-thumbnail-empty">
          {valid ? (
            <button
              className="image-retry"
              onClick={() => {
                setFailed(null);
                setAttempt((a) => a + 1);
              }}
            >
              加载失败
              <br />
              点击重试
            </button>
          ) : (
            '暂无图片'
          )}
        </span>
      )}
      {open &&
        createPortal(
          <div className="overlay image-overlay" onClick={() => setOpen(false)}>
            <section
              className="image-preview"
              role="dialog"
              aria-modal="true"
              aria-label="商品主图预览"
              onClick={(e) => e.stopPropagation()}
            >
              <button autoFocus onClick={() => setOpen(false)}>
                关闭大图
              </button>
              <img src={url} alt={name || '商品主图'} referrerPolicy="no-referrer" />
              <p>{name}</p>
            </section>
          </div>,
          document.body,
        )}
    </>
  );
}
export function ProductInfo({ row, children, showImage = true }) {
  const [copied, setCopied] = useState(false);
  const identifier = row.order_number || row.goods_id;
  async function copy() {
    try {
      await navigator.clipboard.writeText(String(identifier));
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }
  return (
    <div className="goods-with-thumbnail">
      {showImage && <ProductImage url={row.thumbnail_url} name={row.name} />}
      <div
        title={[
          row.name,
          row.category,
          row.code,
          row.goods_id ? `商品 ${row.goods_id}` : null,
          row.order_number ? `订单 ${row.order_number}` : null,
        ]
          .filter(Boolean)
          .join('\n')}
      >
        {children || (
          <>
            <strong>{row.name || '—'}</strong>
            {identifier && (
              <small>
                <button
                  type="button"
                  className="copy-identifier"
                  title={`复制${row.order_number ? '订单号' : '商品 ID'}：${identifier}`}
                  onClick={copy}
                >
                  {copied ? '已复制' : `${row.order_number ? '订单' : '商品'} ${identifier}`}
                </button>
              </small>
            )}
          </>
        )}
        {row.goods_id && row.shop_id && (
          <button
            className="table-action product-overview-link"
            title="商品综合详情"
            aria-label="商品综合详情"
            onClick={() => window.dispatchEvent(new CustomEvent('product-overview', { detail: row }))}
          >
            商品综合详情
          </button>
        )}
      </div>
    </div>
  );
}
