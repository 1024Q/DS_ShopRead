'use client';
import { useEffect } from 'react';
// Close targets: explicit [data-dialog-close] wins; legacy buttons labelled 关闭 / 取消 / × remain a fallback.
const closeText = /^(关闭|取消|×)$/;
const isClose = (b) => b.matches('[data-dialog-close]') || closeText.test(b.textContent.trim());
const closeTarget = (dialog) => {
  const usable = (b) => !b.disabled && b.getClientRects().length;
  return (
    [...dialog.querySelectorAll('[data-dialog-close]')].find(usable) ||
    [...dialog.querySelectorAll('button')].find((b) => usable(b) && closeText.test(b.textContent.trim()))
  );
};
export default function DialogBehavior() {
  useEffect(() => {
    let current = null,
      restore = null,
      frame = 0;
    const dirty = new WeakSet();
    const find = () =>
      [...document.querySelectorAll('.overlay .dialog,[role="dialog"]')]
        .filter((e) => e.getClientRects().length)
        .at(-1);
    const update = () => {
      const next = find();
      if (next === current) return;
      if (current && !next && restore?.isConnected) restore.focus({ preventScroll: true });
      if (next) {
        restore = document.activeElement;
        next.setAttribute('role', 'dialog');
        next.setAttribute('aria-modal', 'true');
        if (!next.classList.contains('ant-modal') && !next.contains(document.activeElement)) {
          const focus = next.querySelector('input,select,button,[tabindex="0"]');
          focus?.focus({ preventScroll: true });
        }
      }
      current = next;
    };
    const key = (e) => {
      if (
        !current ||
        current.classList.contains('ant-modal') ||
        e.defaultPrevented ||
        current.matches('.remote-viewer') ||
        current.querySelector('canvas,.remote-screen')
      )
        return;
      const controls = [...current.querySelectorAll('button,input,select,textarea,a[href],[tabindex="0"]')].filter(
        (n) => !n.disabled && n.getClientRects().length,
      );
      if (e.key === 'Escape') {
        const close = closeTarget(current);
        if (close) {
          e.preventDefault();
          close.click();
        }
      } else if (e.key === 'Tab' && controls.length) {
        const first = controls[0],
          last = controls.at(-1);
        if (e.shiftKey && (document.activeElement === first || !current.contains(document.activeElement))) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    const saved = () => {
      if (current) dirty.delete(current);
    };
    window.addEventListener('dialog-saved', saved);
    const changed = (e) => {
      const dialog = e.target.closest?.('[role="dialog"]');
      if (dialog) dirty.add(dialog);
    };
    const backdrop = (e) => {
      if (
        !e.target.matches?.('.overlay') ||
        !current ||
        !e.target.contains(current) ||
        current.matches('.remote-viewer')
      )
        return;
      const close = closeTarget(current);
      if (!close) return;
      close.click();
    };
    const guardClose = (e) => {
      if (!current || !dirty.has(current)) return;
      const b = e.target.closest?.('button,[data-dialog-close]');
      if (b && current.contains(b) && isClose(b) && !window.confirm('有未保存的输入，确定关闭？')) {
        e.preventDefault();
        e.stopImmediatePropagation();
      }
    };
    document.addEventListener('input', changed);
    document.addEventListener('click', guardClose, true);
    document.addEventListener('click', backdrop, true);
    const observer = new MutationObserver((records) => {
      if (
        !records.some((r) =>
          [...r.addedNodes, ...r.removedNodes].some(
            (n) =>
              n.nodeType === 1 &&
              (n.matches?.('.overlay,[role="dialog"]') || n.querySelector?.('.overlay,[role="dialog"]')),
          ),
        )
      )
        return;
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(update);
    });
    observer.observe(document.body, { subtree: true, childList: true });
    update();
    document.addEventListener('keydown', key);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
      window.removeEventListener('dialog-saved', saved);
      document.removeEventListener('input', changed);
      document.removeEventListener('click', backdrop, true);
      document.removeEventListener('click', guardClose, true);
      document.removeEventListener('keydown', key);
    };
  }, []);
  return null;
}
