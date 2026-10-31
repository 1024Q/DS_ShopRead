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
      '--remote-debugging-port=19376',
      `--user-data-dir=${path.join(root, '.data/ui-scoped-updates')}`,
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19376/json/list')).json()).find((t) => t.type === 'page');
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

    let version = 'A',
      reads = 0,
      commands = 0;
    const fixture = () => ({
      rows: [
        {
          id: 'case1',
          shop_id: 1,
          shop_name: '验证店铺',
          platform: 'pdd',
          kind: 'aftersales',
          module_name: '售后待处理',
          order_number: '验证订单' + version,
          status: '平台介入待商家处理',
          priority: 3,
          updated_at: Date.now() / 1000,
        },
      ],
      total: 1,
      page: 1,
      page_count: 1,
      pending: 0,
      stale: 0,
      sources: [],
    });
    ws.addEventListener('message', async (e) => {
      const m = JSON.parse(e.data);
      if (m.method !== 'Fetch.requestPaused') return;
      const p = m.params;
      try {
        if (p.request.method === 'POST') commands++;
        else reads++;
        await call('Fetch.fulfillRequest', {
          requestId: p.requestId,
          responseCode: 200,
          responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
          body: Buffer.from(JSON.stringify(fixture())).toString('base64'),
        });
      } catch (e) {
        errors.push(e.message);
      }
    });
    await call('Runtime.enable');
    await call('Page.enable');
    await call('Fetch.enable', { patterns: [{ urlPattern: '*/api/attention*' }] });
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source: `if(location.origin==='http://127.0.0.1:3210'){localStorage.clear();sessionStorage.clear();localStorage.setItem('pdd-manager:view','aftersales');localStorage.setItem('pdd-manager:auto-refresh','false');window.__stream=null;window.EventSource=class{static OPEN=1;constructor(){this.readyState=1;this.listeners={};window.__stream=this;}addEventListener(name,fn){this.listeners[name]=fn;}close(){this.readyState=2;}emit(version,changes){this.listeners.database?.({data:JSON.stringify({version,changes})});}};}`,
    });
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait("document.querySelector('.attention-panel')?.innerText.includes('验证订单A')");
    await assert(
      "document.querySelector('.attention-panel').innerText.includes('每 60 秒检查')",
      '60-second focused coverage visible',
    );
    await evaluate("window.__stream.emit('established',[])");
    await pause(700);
    const initial = reads;
    version = 'B';
    await evaluate("window.__stream.emit('unrelated',[{shop_id:1,kind:'goodsdata',platform:'pdd'}])");
    await pause(900);
    if (reads !== initial) throw Error('Unrelated module re-read attention');
    await assert(
      "document.querySelector('.attention-panel').innerText.includes('验证订单A')",
      'unrelated module leaves current list untouched',
    );
    await evaluate("window.__stream.emit('matched',[{shop_id:1,kind:'aftersales',platform:'pdd'}])");
    await wait("document.querySelector('.attention-panel')?.innerText.includes('验证订单B')");
    if (commands) throw Error('Scoped refresh submitted a collection command');
    console.log('PASS relevant committed data refreshes automatically');
    const before = reads;
    version = 'C';
    await evaluate(
      "for(let i=0;i<8;i++)window.__stream.emit('burst'+i,[{shop_id:1,kind:'aftersales',platform:'pdd'}])",
    );
    await wait("document.querySelector('.attention-panel')?.innerText.includes('验证订单C')");
    if (reads - before !== 1) throw Error('Burst notifications were not merged');
    console.log('PASS burst updates coalesced into one read');
    const screenshot = await call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(root, '.data/scoped-updates-ui.png'), Buffer.from(screenshot.data, 'base64'));
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL ATTENTION UI CHECKS PASSED');
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
