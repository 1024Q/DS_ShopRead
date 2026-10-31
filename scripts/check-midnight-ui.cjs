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

    const commands = [],
      reads = [];
    ws.addEventListener('message', async (e) => {
      const m = JSON.parse(e.data);
      if (m.method !== 'Fetch.requestPaused') return;
      const p = m.params;
      try {
        const u = new URL(p.request.url);
        let data;
        if (p.request.method === 'POST') {
          commands.push(u);
          data = { tasks: [{}], accepted: 1 };
        } else {
          reads.push(u);
          data = {
            rows: [],
            metrics: [],
            totals: {},
            sources: [],
            total: 0,
            page: 1,
            page_count: 1,
            groups: [],
            monthly: [],
            shops: [],
            previous_total: 0,
            previous_date: '2026-12-30',
            pending: 0,
            stale: 0,
          };
        }
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
    await call('Fetch.enable', { patterns: [{ urlPattern: '*/api/data/*' }, { urlPattern: '*/api/attention*' }] });
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source: `if(location.origin==='http://127.0.0.1:3210'){localStorage.clear();sessionStorage.clear();localStorage.setItem('pdd-manager:view','overview');localStorage.setItem('pdd-manager:auto-refresh','false');window.testNow=Date.parse('2026-12-31T15:59:50Z');Date.now=()=>window.testNow;window.EventSource=class{static OPEN=1;constructor(){this.readyState=1;}addEventListener(){}close(){}};}`,
    });
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait(
      "document.querySelector('[aria-label=上新统计日期]')?.value==='2026-12-31'&&!!document.querySelector('.ov-attention-link')",
    );
    await pause(500);
    commands.length = 0;
    reads.length = 0;
    await evaluate("window.testNow=Date.parse('2026-12-31T16:00:00.050Z');window.dispatchEvent(new Event('focus'));");
    await wait("document.querySelector('[aria-label=上新统计日期]')?.value==='2027-01-01'");
    await pause(1000);
    for (const kind of ['overview', 'transactions', 'ad_reports', 'products'])
      if (!commands.some((u) => u.pathname === `/api/data/${kind}/sync` && u.searchParams.get('force') === 'true'))
        throw Error('No midnight force sync for ' + kind);
    if (!reads.some((u) => u.pathname === '/api/attention' && u.searchParams.get('business_day') === '2027-01-01'))
      throw Error('Attention not refreshed');
    if (
      !reads.some(
        (u) =>
          u.pathname === '/api/data/products' &&
          u.searchParams.get('date_from') === '2027-01-01' &&
          u.searchParams.get('new_products_year') === '2027',
      )
    )
      throw Error('New arrivals year/date stale');
    await assert(
      "document.querySelector('[aria-label=上新统计年份]').value==='2027'",
      'new year selected automatically',
    );
    const count = commands.length;
    await evaluate("window.dispatchEvent(new Event('focus'));document.dispatchEvent(new Event('visibilitychange'));");
    await pause(600);
    if (commands.length !== count) throw Error('Duplicate midnight collection from focus');
    console.log(
      'PASS midnight refreshes every home data source despite auto-refresh unchecked, new day/year, attention and duplicate event protection',
    );
    await evaluate(
      "window.testNow=Date.parse('2027-01-03T03:00:00Z');document.dispatchEvent(new Event('visibilitychange'));",
    );
    await wait("document.querySelector('[aria-label=上新统计日期]')?.value==='2027-01-03'");
    await pause(600);
    if (commands.length <= count) throw Error('Suspended day catch-up missing');
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL MIDNIGHT HOME UI CHECKS PASSED');
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
