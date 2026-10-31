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
      '--remote-debugging-port=19383',
      `--user-data-dir=${path.join(root, '.data/ui-status-presentation')}`,
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19383/json/list')).json()).find((t) => t.type === 'page');
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
        if (await evaluate(`Boolean(${expression})`)) return;
        await pause(200);
      }
      throw Error('Timed out: ' + expression + '\n' + (await evaluate('document.body.innerText.slice(-2000)')));
    };
    const assert = async (expression, label) => {
      if (!(await evaluate(`Boolean(${expression})`))) throw Error(label);
      console.log('PASS', label);
    };

    const common = {
      shop_id: 1,
      platform: 'pdd',
      shop_name: '测试店铺',
      created_at: '2026-10-11 12:00:00',
      priority: 0,
    };
    const fixtures = {
      products: ['在售中', '已下架', '已售罄'].map((status, i) => ({
        ...common,
        id: String(i),
        goods_id: String(i),
        name: '测试商品 ' + i,
        status,
        price_min: 10,
        price_max: 10,
      })),
      activities: ['报名成功', '报名失败', '审核中'].map((status, i) => ({
        ...common,
        id: String(i),
        goods_id: String(i),
        name: '测试报名商品 ' + i,
        status,
        activity_name: '测试活动',
        price_min: 10,
        price_max: 10,
      })),
      packages: ['发货未揽件', '发货未揽件', '揽件未中转', '中转未派件', '派件未签收', '已签收'].map((status, i) => ({
        ...common,
        id: String(i),
        status,
        tracking_number: 'track' + i,
        order_number: 'order' + i,
        pickup_deadline: status === '发货未揽件' ? new Date(Date.now() + (i ? -1 : 2) * 3600000).toISOString() : null,
      })),
    };
    ws.addEventListener('message', async (e) => {
      const m = JSON.parse(e.data);
      if (m.method !== 'Fetch.requestPaused') return;
      const p = m.params;
      try {
        const u = new URL(p.request.url),
          kind = u.pathname.split('/')[3],
          all = fixtures[kind] || [],
          rows = all.filter((r) => !u.searchParams.get('status') || r.status === u.searchParams.get('status'));
        const data =
          p.request.method === 'POST'
            ? { tasks: [], mode: 'database' }
            : {
                rows,
                total: rows.length,
                page_count: 1,
                statuses: all.map((r) => r.status),
                sources: [
                  { ...common, complete: true, updated_at: new Date().toISOString(), loaded_count: all.length },
                ],
                metrics: [],
                totals: {},
              };
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
    await call('Fetch.enable', { patterns: [{ urlPattern: '*/api/data/*' }] });
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source: `if(location.origin==='http://127.0.0.1:3210'&&!sessionStorage.getItem('test-init')){localStorage.clear();localStorage.setItem('pdd-manager:view','products');localStorage.setItem('pdd-manager:auto-refresh','false');for(const kind of ['products','activities'])localStorage.setItem('pdd-manager:filters:'+kind,JSON.stringify({draft:{status:''}}));sessionStorage.setItem('test-init','yes');}`,
    });
    const statusSelect =
      "[...document.querySelectorAll('.filter-panel label')].find(e=>e.textContent.trim().startsWith('状态')).querySelector('select')";
    const chooseStatus = async (value) => {
      await evaluate(
        `(()=>{const e=${statusSelect};e.value=${JSON.stringify(value)};e.dispatchEvent(new Event('change',{bubbles:true}));})()`,
      );
      await pause(1300);
    };
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait("document.querySelector('.business-heading h2')?.textContent==='商品列表'", 60);
    await wait("document.querySelector('.business-status')");
    await assert(
      `(${statusSelect}).value==='在售中'`,
      'products start with on-sale filter after old preference migration',
    );
    await assert(
      "document.querySelector('.data-table .business-status').classList.contains('tone-green')",
      'on-sale status is green',
    );
    await chooseStatus('');
    await wait("document.querySelectorAll('.data-table tbody tr').length===3");
    await assert(
      "new Set([...document.querySelectorAll('.data-table .business-status')].map(e=>getComputedStyle(e).backgroundColor)).size===3",
      'all-state products have distinct colors',
    );
    await call('Page.reload');
    await wait("document.querySelectorAll('.data-table tbody tr').length===3", 30);
    await assert(`(${statusSelect}).value===''`, 'explicit all-status selection persists');
    await evaluate(
      "[...document.querySelectorAll('.nav-category')].find(x=>x.textContent.includes('营销活动')).click()",
    );
    await wait("document.querySelector('.business-heading h2')?.textContent==='活动报名记录'", 30);
    await wait("document.querySelector('.data-table .business-status')?.textContent==='报名成功'");
    await assert(`(${statusSelect}).value==='报名成功'`, 'activities default to successful enrollment');
    await chooseStatus('');
    await wait("document.querySelectorAll('.data-table tbody tr').length===3");
    await assert(
      "new Set([...document.querySelectorAll('.data-table .business-status')].map(e=>getComputedStyle(e).backgroundColor)).size===3",
      'activity states have distinct colors',
    );
    await evaluate(
      "[...document.querySelectorAll('.nav-category')].find(x=>x.textContent.includes('订单售后')).click()",
    );
    await pause(500);
    await evaluate("[...document.querySelectorAll('.main-tabs button')].find(x=>x.textContent==='包裹中心').click()");
    await wait("document.querySelectorAll('.pickup-countdown').length===2", 30);
    await assert(
      "document.querySelectorAll('.pickup-countdown')[0].textContent==='剩余 2.0 小时'&&document.querySelectorAll('.pickup-countdown')[1].textContent==='已超时 1.0 小时'",
      'pickup countdown distinguishes remaining and overdue hours',
    );
    await assert(
      "[...document.querySelectorAll('.data-table tbody tr')].every(r=>r.children.length===8)",
      'package table columns are aligned',
    );
    for (const theme of ['white', 'dark', 'blue']) {
      await evaluate(`document.documentElement.dataset.theme='${theme}'`);
      await pause(80);
      await assert(
        "new Set([...document.querySelectorAll('.data-table tbody tr')].map(r=>getComputedStyle(r.querySelector('.business-status')).backgroundColor)).size===5",
        'package states distinct in ' + theme,
      );
      const shot = await call('Page.captureScreenshot', { format: 'png' });
      fs.writeFileSync(path.join(root, `.data/status-${theme}.png`), Buffer.from(shot.data, 'base64'));
    }
    if (errors.length) throw Error(JSON.stringify(errors));
    console.log('STATUS UI CHECKS PASSED');
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
