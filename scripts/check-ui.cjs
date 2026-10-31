const fs = require('node:fs');
(async () => {
  const targets = await (await fetch('http://127.0.0.1:19210/json/list')).json();
  const target = targets.find((t) => t.type === 'page' && t.url.startsWith('http://127.0.0.1:3210'));
  if (!target) throw new Error('管理页面未打开');
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', reject, { once: true });
  });
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (e) => {
    const m = JSON.parse(e.data);
    if (pending.has(m.id)) {
      const p = pending.get(m.id);
      pending.delete(m.id);
      m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result);
    }
  });
  const call = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const n = ++id;
      pending.set(n, { resolve, reject });
      ws.send(JSON.stringify({ id: n, method, params }));
    });
  await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
  await new Promise((resolve) => setTimeout(resolve, 4500));
  const result = await call('Runtime.evaluate', { expression: 'document.body.innerText', returnByValue: true });
  console.log(result.result.value);
  const shot = await call('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync('dashboard-preview.png', Buffer.from(shot.data, 'base64'));
  ws.close();
})().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
