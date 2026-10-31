'use client';
import { useEffect, useRef, useState } from 'react';
import { shopLabel } from '../../lib/shop-identity.mjs';
import { appendInput, controlPath } from '../../lib/remote-input.mjs';
import { fetchJson } from '../../lib/fetch-json';

const AUTO_NOTICE = '网络延迟较高，已临时使用流畅画质；延迟恢复后自动切回所选画质。',
  QUALITY_NAMES = { smooth: '流畅', balanced: '均衡', clear: '清晰' };
// Ctrl+Alt+Esc is reserved locally so keyboard users can leave the canvas (Tab/Esc are forwarded to the remote browser).
const isRelease = (e) => e.key === 'Escape' && e.ctrlKey && e.altKey;
const KEY_NAMES = [
  'Backspace',
  'Tab',
  'Escape',
  'Delete',
  'ArrowUp',
  'ArrowDown',
  'ArrowLeft',
  'ArrowRight',
  'Home',
  'End',
  'Enter',
];
export default function RemoteViewer({ shop, onClose, onError, prefix = '' }) {
  const canvas = useRef(null),
    transport = useRef(null),
    pressed = useRef(false),
    point = useRef({ x: 0, y: 0 });
  const overlay = useRef(null),
    screen = useRef(null),
    textInput = useRef(null);
  const [scaleMode, setScaleMode] = useState('fit'),
    [zoom, setZoom] = useState(1),
    [fullscreen, setFullscreen] = useState(false),
    [maximizing, setMaximizing] = useState(false);
  const [sourceSize, setSourceSize] = useState({ width: 1280, height: 720 }),
    [space, setSpace] = useState({ width: 1280, height: 720 });
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) =>
      setSpace({ width: Math.max(1, entry.contentRect.width), height: Math.max(1, entry.contentRect.height) }),
    );
    observer.observe(screen.current);
    const changed = () => setFullscreen(document.fullscreenElement === overlay.current);
    document.addEventListener('fullscreenchange', changed);
    return () => {
      observer.disconnect();
      document.removeEventListener('fullscreenchange', changed);
    };
  }, []);
  const scale = scaleMode === 'fit' ? Math.min(space.width / sourceSize.width, space.height / sourceSize.height) : zoom;
  function changeZoom(delta) {
    setZoom(Math.max(0.25, Math.min(3, Math.round((scale + delta) * 100) / 100)));
    setScaleMode('zoom');
  }
  async function toggleFullscreen() {
    try {
      if (document.fullscreenElement === overlay.current) await document.exitFullscreen();
      else await overlay.current.requestFullscreen();
    } catch {
      setNotice('当前浏览器未允许全屏，可使用适应窗口或浏览器 F11。');
    }
  }
  async function maximize() {
    if (maximizing) return;
    setMaximizing(true);
    try {
      await fetchJson(`/api/${prefix}shops/${shop.id}/maximize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
        signal: AbortSignal.timeout(30000),
      });
      setScaleMode('fit');
      setNotice('');
    } catch (e) {
      setNotice(
        e.status === 404 ? '该采集节点需要更新到新版后才能最大化浏览器' : e.message || '最大化失败，请检查远端浏览器',
      );
    } finally {
      setMaximizing(false);
    }
  }
  const [text, setText] = useState(''),
    [quality, setQuality] = useState('balanced'),
    [autoSmooth, setAutoSmooth] = useState(false),
    [ready, setReady] = useState(false);
  const [status, setStatus] = useState('正在连接'),
    [notice, setNotice] = useState(''),
    [stats, setStats] = useState({ fps: 0, rtt: null, input: null, decode: null, queue: null }),
    [channel, setChannel] = useState('正在协商通道');
  const qualityRef = useRef(quality);
  qualityRef.current = quality;
  useEffect(() => {
    try {
      const saved = localStorage.getItem('pdd-manager:remote-quality');
      if (['smooth', 'balanced', 'clear'].includes(saved)) setQuality(saved);
    } catch {}
  }, []);
  useEffect(() => {
    let active = true,
      socket,
      heartbeat,
      retryTimer,
      fallbackTimer,
      inputTimer,
      ackTimer,
      handshake,
      abort;
    let degraded = false,
      lowSince = 0,
      manualUntil = 0,
      queue = [],
      pending = null,
      serial = 0,
      draws = 0,
      rtt = null,
      attempt = 0,
      legacy = false,
      decoding = false,
      queuedDraw = null,
      inputLatency = null,
      decodeLatency = null,
      queueLatency = null,
      lastStats = performance.now(),
      viewport = { width: 1280, height: 720 };
    const timers = new Set();
    const effective = () => (degraded ? 'smooth' : qualityRef.current);
    const sendQuality = () =>
      wire({ type: 'quality', preset: effective(), binary_frames: true, frame_window: 2, paused: document.hidden });
    const cancel = (t) => {
      clearTimeout(t);
      timers.delete(t);
    };
    const delay = (fn, ms) => {
      const t = setTimeout(() => {
        timers.delete(t);
        if (active) fn();
      }, ms);
      timers.add(t);
      return t;
    };
    function updateStatus(value) {
      if (active) setStatus(value);
    }
    function clearInput() {
      queue = [];
      pending = null;
      cancel(ackTimer);
      cancel(inputTimer);
      inputTimer = null;
      pressed.current = false;
    }
    function wire(value) {
      if (active && socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(value));
        return true;
      }
      return false;
    }
    async function draw(blob, width, height, ack, valid = () => true) {
      if (document.hidden || !valid()) {
        ack?.();
        return;
      }
      if (decoding) {
        queuedDraw?.ack?.();
        queuedDraw = { blob, width, height, ack, valid };
        return;
      }
      decoding = true;
      const decodeStarted = performance.now();
      try {
        const bitmap = await createImageBitmap(blob);
        if (active && valid() && canvas.current) {
          decodeLatency = Math.round(performance.now() - decodeStarted);
          const c = canvas.current;
          if (c.width !== bitmap.width || c.height !== bitmap.height) {
            c.width = bitmap.width;
            c.height = bitmap.height;
          }
          c.getContext('2d').drawImage(bitmap, 0, 0);
          viewport = { width: width || bitmap.width, height: height || bitmap.height };
          setSourceSize((old) =>
            old.width === viewport.width && old.height === viewport.height ? old : { ...viewport },
          );
          draws++;
          setReady(true);
        }
        bitmap.close();
      } catch {
        if (active) setNotice('画面解码失败，正在等待下一帧。');
      } finally {
        decoding = false;
        if (active) ack?.();
        const next = queuedDraw;
        queuedDraw = null;
        if (active && next) void draw(next.blob, next.width, next.height, next.ack, next.valid);
      }
    }
    async function request(event) {
      const r = await fetch(`/api/${prefix}shops/${shop.id}/input`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(event),
        signal: AbortSignal.timeout(8000),
      });
      if (!r.ok) throw Error('操作未确认，请检查画面后重试');
    }
    function flush() {
      if (!active || pending || !queue.length) return;
      if (!legacy && socket?.readyState !== WebSocket.OPEN) {
        queue = [];
        return;
      }
      const id = ++serial,
        events = queue.splice(0, legacy ? 1 : 16);
      pending = { id, at: performance.now() };
      if (legacy) {
        request(events[0])
          .then(() => {
            if (active && pending?.id === id) {
              inputLatency = Math.round(performance.now() - pending.at);
              pending = null;
              flush();
            }
          })
          .catch((e) => {
            if (active && pending?.id === id) {
              clearInput();
              setNotice(e.message);
            }
          });
        return;
      }
      wire({ type: 'input', id, events });
      ackTimer = delay(() => {
        clearInput();
        setNotice('操作响应超时，正在重连；请确认最后一次操作结果。');
        socket?.close();
      }, 8000);
    }
    function send(event) {
      if (!active || (!legacy && socket?.readyState !== WebSocket.OPEN)) return false;
      appendInput(queue, event);
      if (queue.length > 64) {
        clearInput();
        socket?.close();
        setNotice('操作积压，已停止发送并重连。');
        return false;
      }
      const motion = event.type === 'mouseMoved' || event.type === 'wheel';
      if (!motion) {
        cancel(inputTimer);
        inputTimer = null;
      }
      if (!inputTimer)
        inputTimer = delay(
          () => {
            inputTimer = null;
            flush();
          },
          motion ? 30 : 0,
        );
      return true;
    }
    async function fallback() {
      if (!active || !legacy) return;
      if (document.hidden) {
        fallbackTimer = delay(fallback, 1000);
        return;
      }
      abort = new AbortController();
      try {
        const r = await fetch(`/api/${prefix}shops/${shop.id}/screenshot`, {
          cache: 'no-store',
          signal: AbortSignal.any([abort.signal, AbortSignal.timeout(10000)]),
        });
        if (!r.ok) throw Error();
        const blob = await r.blob();
        if (legacy) await draw(blob, undefined, undefined, undefined, () => legacy);
      } catch {
        if (active && legacy) setNotice('画面暂不可用，正在重试。当前保留最后一帧。');
      }
      if (active && legacy) fallbackTimer = delay(fallback, 650);
    }
    function connect() {
      if (!active) return;
      socket = new WebSocket(
        `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}${controlPath(prefix, shop.id)}`,
      );
      socket.binaryType = 'arraybuffer';
      const current = socket;
      handshake = delay(() => current.close(), 12000);
      current.onopen = () => {
        if (!active) {
          current.close();
          return;
        }
        cancel(handshake);
        cancel(fallbackTimer);
        abort?.abort();
        legacy = false;
        clearInput();
        updateStatus('已连接 · 正在获取画面');
        sendQuality();
        wire({ type: 'ping', at: performance.now() });
        clearInterval(heartbeat);
        heartbeat = setInterval(() => wire({ type: 'ping', at: performance.now() }), 2000);
      };
      current.onmessage = async (event) => {
        if (!active || current !== socket) return;
        let m, frame;
        try {
          if (event.data instanceof ArrayBuffer) {
            const length = new DataView(event.data).getUint32(0);
            if (length > 16384 || length + 4 > event.data.byteLength) return;
            m = JSON.parse(new TextDecoder().decode(new Uint8Array(event.data, 4, length)));
            frame = new Blob([event.data.slice(4 + length)], { type: 'image/jpeg' });
          } else m = JSON.parse(event.data);
        } catch {
          return;
        }
        if (m.type === 'frame') {
          attempt = 0;
          queueLatency = Number.isFinite(m.queue_ms) ? m.queue_ms : null;
          const blob =
            frame || new Blob([Uint8Array.from(atob(m.data), (c) => c.charCodeAt(0))], { type: 'image/jpeg' });
          await draw(
            blob,
            m.width,
            m.height,
            () => {
              if (current === socket) wire({ type: 'ack', id: m.id });
            },
            () => current === socket && current.readyState === WebSocket.OPEN,
          );
        } else if (m.type === 'transport') setChannel(m.dedicated ? '独立远控通道' : '兼容共享通道');
        else if (m.type === 'mode') updateStatus(m.mode === 'stream' ? '持续推流' : '截图回退 · 浏览器暂不支持推流');
        else if (m.type === 'pong') rtt = Math.max(0, Math.round(performance.now() - m.at));
        else if (m.type === 'input_ack' && pending?.id === m.id) {
          inputLatency = Math.round(performance.now() - pending.at);
          cancel(ackTimer);
          pending = null;
          flush();
        }
      };
      current.onerror = () => {};
      current.onclose = (event) => {
        cancel(handshake);
        clearInterval(heartbeat);
        clearInput();
        if (!active || current !== socket) return;
        if ([4401, 4404, 4409].includes(event.code)) {
          legacy = false;
          abort?.abort();
          cancel(fallbackTimer);
          updateStatus('连接已停止');
          setNotice(
            event.code === 4409
              ? '该店铺正在另一个窗口中被控制，请先关闭另一个控制窗口。'
              : event.code === 4404
                ? '店铺或远程机器不存在。'
                : '连接认证失败，请检查 API 连接设置。',
          );
          return;
        }
        rtt = null;
        inputLatency = null;
        queueLatency = null;
        setChannel('正在重新连接');
        attempt++;
        updateStatus(attempt >= 3 ? '兼容截图模式' : '连接中断 · 正在重连');
        if (attempt >= 3 && !legacy) {
          legacy = true;
          fallback();
          setNotice('持续推流暂不可用，已回退截图。远程机器升级后将自动重试推流。');
        }
        retryTimer = delay(connect, attempt >= 3 ? 30000 : Math.min(1000 * attempt, 4000));
      };
    }
    transport.current = {
      send,
      quality: (value) => {
        qualityRef.current = value;
        degraded = false;
        lowSince = 0;
        manualUntil = performance.now() + 60000;
        setAutoSmooth(false);
        setNotice((n) => (n === AUTO_NOTICE ? '' : n));
        sendQuality();
      },
      viewport: () => viewport,
    };
    const statsTimer = setInterval(() => {
      if (active) {
        const now = performance.now();
        setStats({
          fps: Math.round((draws * 10000) / (now - lastStats)) / 10,
          rtt,
          input: inputLatency,
          decode: decodeLatency,
          queue: queueLatency,
        });
        lastStats = now;
        if (!degraded && rtt > 500 && qualityRef.current !== 'smooth' && now > manualUntil) {
          degraded = true;
          lowSince = 0;
          sendQuality();
          setAutoSmooth(true);
          setNotice(AUTO_NOTICE);
        } else if (degraded) {
          if (rtt != null && rtt < 250) {
            lowSince ||= now;
            if (now - lowSince >= 15000) {
              degraded = false;
              lowSince = 0;
              sendQuality();
              setAutoSmooth(false);
              setNotice((n) => (n === AUTO_NOTICE ? '' : n));
            }
          } else lowSince = 0;
        }
      }
      draws = 0;
    }, 1000);
    const blur = () => {
      if (pressed.current) {
        send({ type: 'mouseReleased', ...point.current });
        pressed.current = false;
      }
    };
    const visibility = () => {
      blur();
      sendQuality();
    };
    document.addEventListener('visibilitychange', visibility);
    window.addEventListener('blur', blur);
    connect();
    return () => {
      blur();
      active = false;
      queuedDraw = null;
      transport.current = null;
      abort?.abort();
      clearInput();
      for (const timer of timers) clearTimeout(timer);
      cancel(retryTimer);
      clearInterval(heartbeat);
      clearInterval(statsTimer);
      window.removeEventListener('blur', blur);
      document.removeEventListener('visibilitychange', visibility);
      if (socket) {
        socket.onclose = null;
        socket.close();
      }
    };
  }, [shop.id, prefix]);
  function mouse(event, type) {
    if (type === 'mouseMoved' && document.hidden) return;
    if (!ready) return;
    const element = canvas.current,
      rect = element.getBoundingClientRect(),
      v = transport.current?.viewport();
    if (!v) return;
    const position = {
      x: Math.max(0, Math.min(v.width, ((event.clientX - rect.left) * v.width) / rect.width)),
      y: Math.max(0, Math.min(v.height, ((event.clientY - rect.top) * v.height) / rect.height)),
    };
    point.current = position;
    if (type === 'mousePressed') {
      element.focus();
      pressed.current = true;
      element.setPointerCapture(event.pointerId);
    }
    if (type === 'mouseReleased') pressed.current = false;
    transport.current?.send({
      type,
      ...position,
      buttons: pressed.current ? 1 : 0,
      ...(type === 'wheel'
        ? {
            delta_x: Math.max(-2000, Math.min(2000, event.deltaX)),
            delta_y: Math.max(-2000, Math.min(2000, event.deltaY)),
          }
        : {}),
    });
  }
  const mouseRef = useRef(mouse);
  mouseRef.current = mouse;
  useEffect(() => {
    const c = canvas.current;
    const wheel = (e) => {
      e.preventDefault();
      mouseRef.current(e, 'wheel');
    };
    c.addEventListener('wheel', wheel, { passive: false });
    return () => c.removeEventListener('wheel', wheel);
  }, []);
  function changeQuality(value) {
    setQuality(value);
    try {
      localStorage.setItem('pdd-manager:remote-quality', value);
    } catch {}
    transport.current?.quality(value);
  }
  function key(e) {
    if (isRelease(e)) {
      e.preventDefault();
      e.stopPropagation();
      textInput.current?.focus();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (KEY_NAMES.includes(e.key)) {
      e.preventDefault();
      transport.current?.send({ type: 'key', key: e.key });
    } else if (e.key.length === 1) {
      e.preventDefault();
      transport.current?.send({ type: 'text', text: e.key });
    }
  }
  return (
    <div ref={overlay} className="overlay remote-overlay">
      <section className="viewer remote-viewer" role="dialog" aria-modal="true" aria-label="浏览器控制">
        <div className="dialog-head">
          <div>
            <h2>
              {shopLabel(shop)}
              {shop.machine_name ? ' · ' + shop.machine_name : ''} · 浏览器控制
            </h2>
            <p>点击、滚动或拖动画面操作；验证码与扫码由你完成。</p>
          </div>
          <button data-dialog-close onClick={onClose}>
            关闭画面
          </button>
        </div>
        <div className="remote-toolbar">
          <span className="remote-stream-state" role="status">
            {status}
          </span>
          <span title="实际绘制帧率；静止页面没有新画面时可为 0。往返延迟为端到端 ping，操作耗时包含远端执行。">
            实显 {stats.fps} FPS · 往返 {stats.rtt == null ? '—' : stats.rtt + ' ms'} · 操作{' '}
            {stats.input == null ? '—' : stats.input + ' ms'}
          </span>
          <span title="等待是采集端最新帧等待发送的时间；解码是本浏览器处理画面的耗时，并非完整端到端画面延迟。">
            等待 {stats.queue == null ? '—' : stats.queue + ' ms'} · 解码{' '}
            {stats.decode == null ? '—' : stats.decode + ' ms'} · {channel}
          </span>
          <label>
            画质{' '}
            <select
              aria-label="远程画质"
              aria-describedby={autoSmooth ? 'remote-quality-hint' : undefined}
              value={autoSmooth ? 'auto' : quality}
              onChange={(e) => {
                if (e.target.value !== 'auto') changeQuality(e.target.value);
              }}
            >
              {autoSmooth && (
                <option value="auto" disabled>
                  自动流畅（所选：{QUALITY_NAMES[quality]}）
                </option>
              )}
              <option value="smooth">流畅</option>
              <option value="balanced">均衡</option>
              <option value="clear">清晰</option>
            </select>
          </label>
          {autoSmooth && (
            <small id="remote-quality-hint" className="remote-quality-auto">
              延迟较高 · 临时自动流畅，恢复后切回{QUALITY_NAMES[quality]}
            </small>
          )}
          <small>控制期间暂停本店采集 · 关闭后恢复</small>
        </div>
        <div className="remote-display-tools">
          <button aria-pressed={scaleMode === 'fit'} onClick={() => setScaleMode('fit')}>
            适应窗口
          </button>
          <button
            aria-pressed={scaleMode === 'zoom' && zoom === 1}
            onClick={() => {
              setScaleMode('zoom');
              setZoom(1);
            }}
          >
            100% 原始大小
          </button>
          <button aria-label="缩小远程画面" disabled={scale <= 0.25} onClick={() => changeZoom(-0.1)}>
            −
          </button>
          <span aria-label="当前缩放比例">{Math.round(scale * 100)}%</span>
          <button aria-label="放大远程画面" disabled={scale >= 3} onClick={() => changeZoom(0.1)}>
            ＋
          </button>
          <button onClick={toggleFullscreen}>{fullscreen ? '退出全屏' : '全屏'}</button>
          <button disabled={!ready || maximizing} onClick={maximize}>
            {maximizing ? '调整中…' : '远端浏览器最大化'}
          </button>
        </div>
        {notice && (
          <p className="error" role="alert">
            {notice}
          </p>
        )}
        <div ref={screen} className="screen remote-screen">
          <div className="remote-canvas-stage">
            <canvas
              ref={canvas}
              aria-label="店铺浏览器实时画面"
              aria-describedby="remote-keyboard-hint"
              tabIndex={0}
              style={{
                display: ready ? 'block' : 'none',
                width: Math.max(1, Math.floor(sourceSize.width * scale)),
                height: Math.max(1, Math.floor(sourceSize.height * scale)),
              }}
              onKeyDown={key}
              onContextMenu={(e) => e.preventDefault()}
              onPointerDown={(e) => mouse(e, 'mousePressed')}
              onPointerMove={(e) => mouse(e, 'mouseMoved')}
              onPointerUp={(e) => mouse(e, 'mouseReleased')}
              onPointerCancel={(e) => mouse(e, 'mouseReleased')}
            />
            {!ready && <p>正在获取浏览器画面…</p>}
          </div>
        </div>
        <p id="remote-keyboard-hint" className="remote-keyboard-hint">
          画面获得焦点后，键盘（含 Tab、Esc）会发送到远程浏览器；按 <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Esc</kbd>{' '}
          释放焦点到下方输入框。
        </p>
        <div className="input-row">
          <input
            ref={textInput}
            aria-label="输入到远程浏览器的文字"
            value={text}
            onChange={(e) => setText(e.target.value)}
            maxLength={1024}
            placeholder="先点击远程输入框，再在这里输入中文或验证码"
          />
          <button
            onClick={() => {
              if (transport.current?.send({ type: 'text', text })) setText('');
            }}
          >
            输入到浏览器
          </button>
          {[
            ['Enter', '回车'],
            ['Backspace', '退格'],
            ['Tab', 'Tab'],
            ['Escape', 'Esc'],
          ].map(([key, label]) => (
            <button key={key} onClick={() => transport.current?.send({ type: 'key', key })}>
              {label}
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}
