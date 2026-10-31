'use client';
import { useRef } from 'react';
import { Modal } from 'antd';
// Reuse the original close action so busy and unsaved-input guards still apply.
export default function AdminDialog({ children, label = '详情' }) {
  const root = useRef(null);
  const close = () => {
    const buttons = [...(root.current?.querySelectorAll('button') || [])];
    const target = buttons.find(
      (b) =>
        !b.disabled &&
        (b.hasAttribute('data-dialog-close') ||
          /^(关闭.*|取消|×)$/.test((b.getAttribute('aria-label') || b.textContent).trim())),
    );
    target?.click();
  };
  return (
    <Modal
      open
      centered
      footer={null}
      closable={false}
      onCancel={close}
      width={1000}
      className="admin-dialog"
      aria-label={label}
      styles={{ body: { maxHeight: 'calc(100dvh - 80px)', overflowY: 'auto' } }}
    >
      <div ref={root}>{children}</div>
    </Modal>
  );
}
