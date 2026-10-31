const { spawn } = require('child_process'),
  fs = require('fs');
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const chrome = spawn(
    '.browsers/chrome-win64/chrome.exe',
    [
      '--headless=new',
      '--no-first-run',
      '--window-size=1600,1100',
      '--remote-debugging-port=19378',
      '--user-data-dir=D:/Project/pddauto/tmp/sync-ui-test',
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws;
  try {
    let tab;
    for (let i = 0; i < 40; i++) {
      try {
        tab = (await (await fetch('http://127.0.0.1:19378/json/list')).json()).find((t) => t.type === 'page');
        if (tab) break;
      } catch {}
      await pause(200);
    }
    ws = new WebSocket(tab.webSocketDebuggerUrl);
    await new Promise((r) => (ws.onopen = r));
    let seq = 0;
    const pending = new Map(),
      errors = [];
    let sourceRequests = 0;
    const call = (method, params = {}) =>
      new Promise((resolve, reject) => {
        const id = ++seq;
        pending.set(id, { resolve, reject });
        ws.send(JSON.stringify({ id, method, params }));
      });
    const shop = { id: 1, name: '测试旗舰店', platform: 'pdd', state: 'logged_in', operators: ['张三'] };
    const rows = Array.from({ length: 30 }, (_, i) => ({
      ...shop,
      shop_id: i + 1,
      shop_name: '测试旗舰店' + i,
      kind: 'tickets',
      data_status: i === 0 ? 'error' : 'ready',
      updated_at: new Date().toISOString(),
      loaded_count: 200,
      scope: {},
      cadence: 'realtime',
      frequency_name: '实时采集',
      interval_seconds: 60,
      error: i === 0 ? '浏览器连接超时' : null,
      failures: i === 0 ? 2 : 0,
      repair_advice: i === 0 ? [{ automatic: true, reason: '连接超时', action: '等待自动重试' }] : [],
    }));
    ws.onmessage = async (e) => {
      const m = JSON.parse(e.data);
      if (pending.has(m.id)) {
        const p = pending.get(m.id);
        pending.delete(m.id);
        m.error ? p.reject(Error(m.error.message)) : p.resolve(m.result);
      }
      if (m.method === 'Runtime.exceptionThrown')
        errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
      if (m.method === 'Fetch.requestPaused') {
        const u = new URL(m.params.request.url);
        let data = {};
        if (u.pathname === '/api/connection') data = { connected: true, cacheScope: 'sync-test' };
        if (u.pathname === '/api/shops') data = [shop];
        if (u.pathname === '/api/browsers' || u.pathname === '/api/operators' || u.pathname === '/api/nodes') data = [];
        if (u.pathname.includes('/system/progress') || u.pathname.includes('/system/summary'))
          data = {
            summary: { running: 0, queued: 0, errors: 1 },
            collection: { enabled: true, realtime_seconds: 60 },
            collector_online: true,
          };
        if (u.pathname === '/api/system/sources') {
          sourceRequests++;
          data = {
            snapshot_complete: true,
            rows,
            total: 470,
            page: Number(u.searchParams.get('page') || 1),
            page_count: 16,
            retry_count: 1,
            counts: { all: 470, issues: 1, active: 0, ready: 469 },
            kinds: ['tickets'],
          };
        }
        if (u.pathname === '/api/home/progress') data = { rows: [], total_modules: 0 };
        await call('Fetch.fulfillRequest', {
          requestId: m.params.requestId,
          responseCode: 200,
          responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
          body: Buffer.from(JSON.stringify(data)).toString('base64'),
        });
      }
    };
    const ev = async (expression) =>
      (await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })).result?.value;
    await call('Runtime.enable');
    await call('Page.enable');
    await call('Network.setCookie', { name: 'ecmp_center', value: 'synthetic-ui-test', url: 'http://127.0.0.1:3212' });
    await call('Fetch.enable', { patterns: [{ urlPattern: '*/api/*' }] });
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source:
        "localStorage.clear();sessionStorage.clear();localStorage.setItem('pdd-manager:view','sources');window.EventSource=class{constructor(){this.readyState=1;}addEventListener(){}close(){}};",
    });
    await call('Page.navigate', { url: 'http://127.0.0.1:3212/?view=sources' });
    for (let i = 0; i < 80; i++) {
      if (await ev("!!document.querySelector('.sync-compact-table')")) break;
      await pause(200);
    }
    const metrics = await ev(
      "({rows:document.querySelectorAll('.sync-compact-table>tbody>tr').length,heights:[...document.querySelectorAll('.sync-compact-table>tbody>tr')].map(x=>Math.round(x.getBoundingClientRect().height)),timers:document.querySelectorAll('[role=timer]').length})",
    );
    if (metrics.rows !== 30)
      console.log(
        'diagnostics',
        errors,
        await ev('location.href'),
        await ev('document.documentElement.outerHTML.slice(-2000)'),
      );
    if (metrics.rows !== 30)
      throw Error('Rows ' + JSON.stringify(metrics) + ' ' + (await ev('document.body.innerText.slice(-900)')));
    console.log(metrics);
    await pause(250);
    const requestsBefore = sourceRequests;
    const filterMs = await ev(
      `(async()=>{const t=performance.now();[...document.querySelectorAll('.sync-overview button')].find(b=>b.innerText.includes('异常')).click();await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));if(document.querySelectorAll('.sync-compact-table>tbody>tr').length!==1)throw Error('Issue filter mismatch');return performance.now()-t;})()`,
    );
    if (sourceRequests !== requestsBefore) throw Error('Filtering must not fetch remote data');
    console.log('PASS instant local filter', Math.round(filterMs), 'ms, no network request');
    await ev(
      "[...document.querySelectorAll('.sync-overview button')].find(b=>b.innerText.includes('全部数据源')).click()",
    );
    await pause(100);
    await ev("document.querySelector('.source-row-actions button').click()");
    await pause(200);
    if (!(await ev("!!document.querySelector('dialog[open]')"))) throw Error('Dialog missing');
    await call('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
    await pause(100);
    if (await ev("!!document.querySelector('dialog[open]')")) throw Error('Escape close failed');
    await ev("document.querySelector('.sync-center .filter-panel').scrollIntoView({block:'start'})");
    await pause(200);
    fs.writeFileSync(
      'tmp/sync-compact.png',
      Buffer.from((await call('Page.captureScreenshot', { format: 'png' })).data, 'base64'),
    );
    if (errors.length) throw Error(errors.join(';'));
    console.log('PASS compact table, lazy dialog, Escape close; screenshot tmp/sync-compact.png');
  } finally {
    ws?.close();
    chrome.kill();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
