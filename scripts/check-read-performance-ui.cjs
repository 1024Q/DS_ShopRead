// Isolated browser with synthetic intercepted responses; never submits collection jobs.
const { spawn } = require('node:child_process');
const fs = require('node:fs'),
  path = require('node:path');
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const root = path.resolve(__dirname, '..');
  const chrome = spawn(
    path.join(root, '.browsers/chrome-win64/chrome.exe'),
    [
      '--headless=new',
      '--no-first-run',
      '--window-size=1560,1100',
      '--remote-debugging-port=19371',
      `--user-data-dir=${path.join(root, '.data/ui-read-performance')}`,
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19371/json/list')).json()).find((t) => t.type === 'page');
        if (target) break;
      } catch {}
      await pause(250);
    }
    if (!target) throw Error('Browser unavailable');
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((r, j) => {
      ws.addEventListener('open', r, { once: true });
      ws.addEventListener('error', j, { once: true });
    });
    let id = 0;
    const pending = new Map(),
      errors = [],
      requests = [];
    ws.addEventListener('message', (e) => {
      const m = JSON.parse(e.data),
        p = pending.get(m.id);
      if (p) {
        pending.delete(m.id);
        m.error ? p.reject(Error(m.error.message)) : p.resolve(m.result);
      }
      if (m.method === 'Runtime.exceptionThrown')
        errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    });
    call = (method, params = {}) =>
      new Promise((resolve, reject) => {
        const n = ++id;
        pending.set(n, { resolve, reject });
        ws.send(JSON.stringify({ id: n, method, params }));
      });
    const evaluate = async (expression) => {
      const r = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
      return r.result.value;
    };
    const wait = async (expression, seconds = 30) => {
      for (let n = 0; n < seconds * 5; n++) {
        if (await evaluate(expression)) return;
        await pause(200);
      }
      throw Error('Timed out: ' + expression + '\n' + (await evaluate('document.body.innerText.slice(-2000)')));
    };
    const assert = async (expression, label) => {
      if (!(await evaluate(expression))) throw Error(label);
      console.log('PASS', label);
    };

    const reads = [],
      commands = [],
      requestsSeen = [];
    let held = false,
      waiting = [],
      version = 'A';
    const fixture = () => ({
      rows: [
        {
          id: 'performance-review',
          shop_id: 1,
          shop_name: '性能验证店铺',
          platform: 'pdd',
          name: '性能验证商品',
          content: '性能验证评价' + version,
          stars: 3,
          replied: false,
          created_at: '2026-10-10 09:00:00',
        },
      ],
      sources: [
        {
          shop_id: 1,
          shop_name: '性能验证店铺',
          platform: 'pdd',
          updated_at: '2026-10-10T09:00:00+08:00',
          complete: true,
          loaded_count: 1,
          source_total: 1,
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
      statuses: [],
      metrics: [],
      totals: {},
    });
    const fulfill = (requestId, data, status = 200) =>
      call('Fetch.fulfillRequest', {
        requestId,
        responseCode: status,
        responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
        body: Buffer.from(JSON.stringify(data)).toString('base64'),
      });
    ws.addEventListener('message', async (e) => {
      const m = JSON.parse(e.data);
      if (m.method === 'Network.requestWillBeSent') requestsSeen.push(m.params.request.url);
      if (m.method === 'Fetch.requestPaused') {
        const p = m.params,
          url = new URL(p.request.url);
        try {
          if (url.pathname.endsWith('/sync')) {
            commands.push(url.href);
            await fulfill(p.requestId, { tasks: [] }, 202);
          } else {
            reads.push(url.href);
            if (held) waiting.push(p.requestId);
            else await fulfill(p.requestId, fixture());
          }
        } catch (e) {
          if (!/Invalid InterceptionId/i.test(e.message)) errors.push(e.message);
        }
      }
    });
    await call('Runtime.enable');
    await call('Page.enable');
    await call('Network.enable');
    await call('Fetch.enable', { patterns: [{ urlPattern: '*/api/data/reviews*' }] });
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source:
        "if(location.origin==='http://127.0.0.1:3210'){localStorage.setItem('pdd-manager:view','reviews');localStorage.removeItem('pdd-manager:filters:reviews');localStorage.setItem('pdd-manager:auto-refresh','false');}",
    });
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait("document.body.innerText.includes('性能验证评价A')");
    if (commands.length) throw Error('Saved data should not submit a collection task');
    console.log('PASS database first without collection command');
    if (requestsSeen.some((u) => new URL(u).pathname === '/api/system/status'))
      throw Error('Detailed status fetched outside source page');
    if (!requestsSeen.some((u) => new URL(u).pathname === '/api/system/summary'))
      throw Error('Missing lightweight header request');
    console.log('PASS lightweight header');
    held = true;
    await call('Page.reload');
    await wait(
      "document.body.innerText.includes('性能验证评价A')&&document.body.innerText.includes('当前显示上次结果')",
    );
    if (!waiting.length) throw Error('Expected fresh request to remain pending');
    console.log('PASS preview displayed during hard refresh before database response');
    version = 'B';
    held = false;
    for (const id of waiting.splice(0))
      try {
        await fulfill(id, fixture());
      } catch (e) {
        if (!/Invalid InterceptionId/i.test(e.message)) throw e;
      }
    await wait(
      "document.body.innerText.includes('性能验证评价B')&&!document.body.innerText.includes('当前显示上次结果')",
    );
    console.log('PASS fresh result replaces preview');
    await evaluate("[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='立即同步').click()");
    await pause(1200);
    if (!commands.some((u) => u.includes('force=true'))) throw Error('Manual force command missing');
    console.log('PASS manual sync preserved');
    await evaluate("document.querySelector('.global-sync-info button').click()");
    await wait(
      "document.body.innerText.includes('同步与数据源')&&document.querySelectorAll('.sync-table tbody tr').length>0",
    );
    if (!requestsSeen.some((u) => new URL(u).pathname === '/api/system/status'))
      throw Error('Detail page did not fetch full status');
    console.log('PASS detailed status on demand');
    const screenshot = await call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(root, '.data/read-performance-ui.png'), Buffer.from(screenshot.data, 'base64'));
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL READ PERFORMANCE UI CHECKS PASSED');
  } finally {
    if (call)
      try {
        await call('Browser.close');
      } catch {}
    if (ws) ws.close();
    chrome.kill();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
