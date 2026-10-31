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
      '--remote-debugging-port=19381',
      `--user-data-dir=${path.join(root, '.data/ui-consolidation')}`,
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19381/json/list')).json()).find((t) => t.type === 'page');
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
      if (!(await evaluate(expression))) throw Error(label);
      console.log('PASS', label);
    };

    const history = {
      rows: [
        {
          id: 1,
          shop_id: 1,
          shop_label: '拼多多 · 测试店铺',
          kind: 'evidence',
          record_id: 'case-a',
          change: 'resolved',
          created: Date.now() / 1000,
          context: { order_number: 'order-a', status: '待举证' },
        },
      ],
      next_before: null,
    };
    const fleet = {
      date: '2026-10-11',
      unique_shops: 2,
      totals: { sales: 120, orders: 3, ad_spend: 12, pending_aftersales: 2 },
      coverage: { sales: 1, orders: 1, ad_spend: 1, pending_aftersales: 1 },
      machines: [
        { node_id: 0, name: '主服务', online: true, updated_at: Date.now() / 1000 },
        {
          node_id: 9,
          name: '远程测试机',
          online: false,
          error: '远端暂不可用，保留上次结果',
          updated_at: Date.now() / 1000 - 600,
        },
      ],
      rows: [
        {
          id: '0:1',
          node_id: 0,
          machine_name: '主服务',
          shop_id: 1,
          name: '同名店',
          platform: 'pdd',
          metrics: { sales: 120, orders: 3 },
          online: true,
          current_day: true,
          supported: true,
          attention: 2,
          operator: '张三',
        },
        {
          id: '9:1',
          node_id: 9,
          machine_name: '远程测试机',
          shop_id: 1,
          name: '同名店',
          platform: 'taobao',
          metrics: { sales: 999, orders: 5 },
          online: false,
          current_day: true,
          supported: true,
          attention: null,
          operator: '李四',
        },
      ],
    };
    ws.addEventListener('message', async (e) => {
      const m = JSON.parse(e.data);
      if (m.method === 'Network.requestWillBeSent')
        requests.push({ url: m.params.request.url, method: m.params.request.method });
      if (m.method !== 'Fetch.requestPaused') return;
      const p = m.params;
      try {
        await call('Fetch.fulfillRequest', {
          requestId: p.requestId,
          responseCode: 200,
          responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
          body: Buffer.from(JSON.stringify(p.request.url.includes('/attention/history') ? history : fleet)).toString(
            'base64',
          ),
        });
      } catch (e) {
        errors.push(e.message);
      }
    });
    await call('Runtime.enable');
    await call('Page.enable');
    await call('Network.enable');
    await call('Fetch.enable', {
      patterns: ['*/api/fleet*', '*/api/attention/history*'].map((urlPattern) => ({ urlPattern })),
    });
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source: `if(location.origin==='http://127.0.0.1:3210'){localStorage.clear();sessionStorage.clear();localStorage.setItem('pdd-manager:view','overview');localStorage.setItem('pdd-manager:auto-refresh','false');}`,
    });
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait("document.querySelector('.home-refresh-summary')?.textContent.includes('家今日已更新')", 60);
    await pause(3000);
    await assert(
      "document.querySelector('.global-auto-label').textContent==='界面已暂停'",
      'persisted pause is visible',
    );
    let start = requests.length;
    await evaluate("window.dispatchEvent(new CustomEvent('database-updated',{detail:{reset:true}}))");
    await pause(2000);
    const business = (r) => /\/api\/(data\/|home\/progress)/.test(r.url);
    if (requests.slice(start).some(business))
      throw Error('Paused business data still requested: ' + JSON.stringify(requests.slice(start).filter(business)));
    console.log('PASS paused state suppresses database events');
    start = requests.length;
    await evaluate("Array.from(document.querySelectorAll('button')).find(x=>x.textContent==='刷新已存数据').click()");
    await pause(3500);
    if (!requests.slice(start).some((r) => business(r) && r.method === 'GET')) throw Error('Manual read missing');
    if (requests.slice(start).some((r) => r.method === 'POST' && r.url.includes('/sync')))
      throw Error('Read-only refresh submitted collection');
    console.log('PASS manual refresh reads without collection');
    await evaluate("document.querySelector('.global-auto-label').click()");
    await pause(2500);
    await evaluate("document.querySelector('.new-products-panel .module-collapse').click()");
    await pause(800);
    start = requests.length;
    await evaluate(
      "window.dispatchEvent(new CustomEvent('database-updated',{detail:{changes:[{kind:'products',shop_id:0}]}}))",
    );
    await pause(1500);
    if (requests.slice(start).some((r) => r.url.includes('/api/data/products')))
      throw Error('Collapsed product panel still requests');
    console.log('PASS collapsed module stops data requests');
    await evaluate("document.querySelector('.new-products-panel .module-collapse').click()");
    await wait("document.querySelector('.arrival-months')");
    await evaluate("document.querySelector('.attention-notice button').click()");
    await wait("document.body.textContent.includes('重点变化记录')&&document.body.textContent.includes('order-a')");
    await assert("document.body.textContent.includes('已解除')", 'notification history distinguishes resolved records');
    await evaluate(
      "Array.from(document.querySelectorAll('.nav-category')).find(x=>x.textContent.includes('系统管理')).click()",
    );
    await wait("document.querySelector('.fleet-dashboard')");
    await wait("document.querySelectorAll('.fleet-dashboard tbody tr').length===2");
    await assert(
      "document.querySelector('.fleet-dashboard').textContent.includes('远程测试机')&&document.querySelector('.fleet-dashboard').textContent.includes('离线')",
      'offline machine retains labelled snapshot',
    );
    await assert(
      "document.querySelector('.fleet-kpis article strong').textContent==='120'",
      'fleet total excludes offline stored amount',
    );
    for (const theme of ['white', 'dark', 'blue']) {
      await evaluate(`document.documentElement.dataset.theme='${theme}'`);
      await pause(100);
      const shot = await call('Page.captureScreenshot', { format: 'png' });
      fs.writeFileSync(path.join(root, `.data/fleet-${theme}.png`), Buffer.from(shot.data, 'base64'));
    }
    await evaluate(
      "Array.from(document.querySelectorAll('.main-tabs button')).find(x=>x.textContent==='系统设置').click()",
    );
    await wait("document.querySelector('[aria-label=采集策略平台]')");
    await evaluate(
      "(()=>{const el=document.querySelector('[aria-label=采集策略平台]');el.value='taobao';el.dispatchEvent(new Event('change',{bubbles:true}));})()",
    );
    await wait("document.querySelector('.module-settings')&&document.body.textContent.includes('淘宝 · 模块采集方式')");
    await assert("document.body.textContent.includes('已保存生效')", 'saved effective platform intervals shown');
    await wait(
      "document.body.textContent.includes('存储容量')||document.body.textContent.includes('磁盘空间不足预警')",
    );
    if (errors.length) throw Error(JSON.stringify(errors));
    console.log('ALL CONSOLIDATION UI CHECKS PASSED');
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
