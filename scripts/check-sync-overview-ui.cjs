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
      '--remote-debugging-port=19375',
      `--user-data-dir=${path.join(root, '.data/ui-attention-link')}`,
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19375/json/list')).json()).find((t) => t.type === 'page');
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

    const commands = [];
    const src = (id, extra = {}) => ({
      shop_id: id,
      shop_name: '测试店铺' + id,
      platform: 'pdd',
      kind: 'reviews',
      data_status: 'error',
      error: '临时异常',
      scope: { date_from: '2026-09-01', date_to: '2026-09-02', _exact_dates: true },
      ...extra,
    });
    const sources = [
      src(5),
      src(6, { kind: 'evidence', scope: { _attention: true } }),
      src(7, { waiting_login: true }),
      src(8, { syncing: true, data_status: 'running' }),
      src(10, { error: null, data_status: 'ready', updated_at: 1 }),
      src(11, { kind: 'tax', scope: { period: '2026-09' } }),
    ];
    ws.addEventListener('message', async (e) => {
      const m = JSON.parse(e.data);
      if (m.method !== 'Fetch.requestPaused') return;
      const p = m.params;
      try {
        const url = new URL(p.request.url);
        let data;
        if (p.request.method === 'POST') {
          commands.push(p.request.url);
          await pause(150);
          data = { tasks: [{}], accepted: 1 };
        } else if (url.pathname.includes('/system/'))
          data = { sources, summary: { errors: 4, running: 1, queued: 0 }, collector_online: true };
        else data = { rows: [], total: 12, pending: 0, stale: 0, sources: [] };
        await call('Fetch.fulfillRequest', {
          requestId: p.requestId,
          responseCode: 200,
          responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
          body: Buffer.from(JSON.stringify(data)).toString('base64'),
        });
      } catch (e) {
        errors.push(e.message);
      }
    });
    await call('Runtime.enable');
    await call('Page.enable');
    await call('Fetch.enable', {
      patterns: [
        { urlPattern: '*/api/system/status*' },
        { urlPattern: '*/api/system/summary*' },
        { urlPattern: '*/api/attention*' },
        { urlPattern: '*/api/data/*/sync*' },
      ],
    });
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source: `if(location.origin==='http://127.0.0.1:3210'){localStorage.clear();sessionStorage.clear();localStorage.setItem('pdd-manager:view','sources');localStorage.setItem('pdd-manager:auto-refresh','false');}`,
    });
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait("!!document.querySelector('.sync-retry-toolbar button')");
    await assert(
      "document.querySelector('.sync-retry-toolbar button').textContent.includes('（3）')",
      'only failed eligible sources counted',
    );
    await evaluate(
      "(()=>{const s=document.querySelector('[aria-label=数据源模块]');Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(s,'reviews');s.dispatchEvent(new Event('change',{bubbles:true}));})()",
    );
    await wait("document.querySelector('.sync-retry-toolbar button').textContent.includes('（1）')");
    await evaluate(
      "document.querySelector('.sync-retry-toolbar button').click();document.querySelector('.sync-retry-toolbar button').click()",
    );
    await wait("document.querySelector('.sync-retry-result')?.textContent.includes('本次提交完成')");
    if (commands.length !== 1 || !commands[0].includes('shop_id=5') || !commands[0].includes('exact_dates=true'))
      throw Error('Filtered retry or duplicate protection failed: ' + commands.join(','));
    await evaluate(
      "(()=>{const s=document.querySelector('[aria-label=数据源模块]');Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(s,'');s.dispatchEvent(new Event('change',{bubbles:true}));})()",
    );
    await wait("document.querySelector('.sync-retry-toolbar button').textContent.includes('（3）')");
    await evaluate("document.querySelector('.sync-retry-toolbar button').click()");
    await wait(
      "document.querySelector('.sync-retry-result')?.textContent.includes('已提交 3 项')&&!document.querySelector('.sync-retry-toolbar button').disabled",
    );
    if (
      commands.length !== 4 ||
      !commands.some((u) => u.includes('/api/attention/sync')) ||
      !commands.some((u) => u.includes('period=2026-09'))
    )
      throw Error('Batch paths incorrect');
    console.log('PASS batch retry, filters, duplicate-click protection, date and period preservation');
    let shot = await call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(root, '.data/sync-retry-ui.png'), Buffer.from(shot.data, 'base64'));
    await evaluate("document.querySelector('button[aria-label=数据中心]').click()");
    await wait("Array.from(document.querySelectorAll('.main-tabs button')).some(b=>b.textContent==='实时概览')");
    await evaluate(
      "Array.from(document.querySelectorAll('.main-tabs button')).find(b=>b.textContent==='实时概览').click()",
    );
    await wait("document.querySelector('.ov-attention-link strong')?.textContent==='12'");
    await assert(
      "!document.querySelector('.ov-dual-revenue')&&!document.querySelector('.overview-dashboard').innerText.includes('平台支付口径')&&!!document.querySelector('.ov-realtime-revenue')",
      'home retains only realtime revenue',
    );
    await assert(
      "!document.querySelector('.overview-dashboard .attention-panel')&&document.querySelector('.ov-attention .collapsible-body').firstElementChild.classList.contains('ov-attention-link')",
      'priority count first, no details list on overview',
    );
    for (const theme of ['white', 'dark', 'blue']) {
      await evaluate(`document.documentElement.dataset.theme='${theme}'`);
      await pause(150);
      await assert(
        "(()=>{const r=document.querySelector('.ov-attention').getBoundingClientRect();return [...document.querySelectorAll('.ov-attention button')].every(b=>{const x=b.getBoundingClientRect();return x.right<=r.right+1&&x.left>=r.left-1;});})()",
        theme + ' action card controls fit',
      );
      const shot = await call('Page.captureScreenshot', { format: 'png' });
      fs.writeFileSync(path.join(root, `.data/overview-actions-${theme}.png`), Buffer.from(shot.data, 'base64'));
    }
    await call('Emulation.setDeviceMetricsOverride', {
      width: 1100,
      height: 1000,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await pause(300);
    await assert(
      "getComputedStyle(document.querySelector('.ov-attention .collapsible-body')).gridTemplateColumns.split(' ').length===2",
      'medium screen action card uses two columns',
    );
    await evaluate("document.querySelector('.ov-attention .module-collapse').click()");
    await assert("document.querySelector('.ov-attention .collapsible-body').hidden", 'action card collapses');
    await evaluate(
      "document.querySelector('.ov-attention .module-collapse').click();document.querySelector('.ov-attention-link').click()",
    );
    await wait("document.querySelector('.business-heading h2')?.textContent==='重点待办中心'");
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL SYNC RETRY AND OVERVIEW UI CHECKS PASSED');
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
