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
      '--remote-debugging-port=19354',
      `--user-data-dir=${path.join(root, '.data/ui-taobao-modules')}`,
      'http://127.0.0.1:3210/',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let i = 0; i < 40; i++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19354/json/list')).json()).find((t) => t.type === 'page');
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
      if (m.method === 'Network.requestWillBeSent') requests.push(m.params.request.url);
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
    const wait = async (expression, seconds = 45) => {
      for (let i = 0; i < seconds * 2; i++) {
        if (await evaluate(expression)) return;
        await pause(500);
      }
      throw Error('Timed out: ' + expression + '\n' + (await evaluate('document.body.innerText.slice(0,2000)')));
    };
    const assert = async (expression, label) => {
      if (!(await evaluate(expression))) throw Error(label);
      console.log('PASS', label);
    };
    const capture = async (name) => {
      await evaluate("[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='关闭画面')?.click()");
      await pause(300);
      fs.writeFileSync(
        path.join(root, '.data', name),
        Buffer.from((await call('Page.captureScreenshot', { format: 'png' })).data, 'base64'),
      );
    };
    await call('Runtime.enable');
    await call('Network.enable');
    await call('Page.enable');
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait("!!document.querySelector('.workspace-sidebar')");
    const sid = await evaluate("fetch('/api/shops').then(r=>r.json()).then(s=>s.find(x=>x.platform==='taobao').id)");

    const setView = async (kind, extra = {}) => {
      await evaluate(
        `localStorage.setItem('pdd-manager:view',${JSON.stringify(kind)});localStorage.setItem('pdd-manager:auto-refresh','false');localStorage.setItem('pdd-manager:filters:'+${JSON.stringify(kind)},JSON.stringify({draft:${JSON.stringify({ shop_id: String(sid), trade_range: 'realtime', ...extra })}}));location.reload()`,
      );
      await wait('!!document.querySelector(\'[aria-label="业务平台筛选"] option[value="taobao"]\')');
      await evaluate(
        "(()=>{const e=document.querySelector('[aria-label=\"业务平台筛选\"]');e.value='taobao';e.dispatchEvent(new Event('change',{bubbles:true}));})()",
      );
      await pause(1000);
      await evaluate("[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='关闭画面')?.click()");
    };
    await setView('goodsdata', { trade_range: 'realtime' });
    await wait(
      "!!document.querySelector('.taobao-traffic .metric-card strong')&&document.querySelector('.taobao-traffic .metric-card strong').textContent!=='—'",
    );
    await assert(
      "document.querySelector('.taobao-traffic').textContent.includes('店铺访客')&&!!document.querySelector('.taobao-traffic polyline')",
      'Live traffic metrics and hourly graph from database',
    );
    await capture('taobao-traffic-final.png');
    await setView('aftersales');
    await wait("document.querySelector('.business-panel tbody')?.textContent.includes('运输停滞')");
    await assert(
      "document.querySelector('.business-panel tbody').textContent.includes('预估赔偿')&&!!document.querySelector('.business-panel tr.priority-row')",
      'Taobao package warning highlights and compensation label',
    );
    await assert(
      "[...document.querySelectorAll('.business-panel tbody tr')].every(r=>r.textContent.includes('淘宝'))",
      'Platform filter excludes Pinduoduo orders',
    );
    await capture('taobao-packages-final.png');
    await setView('invoices', { status: '待开票' });
    await wait("document.querySelector('.business-panel tbody')?.textContent.includes('待开票')");
    await assert(
      "document.querySelector('.business-panel').textContent.includes('给买家开票')",
      'Buyer invoice applications with source coverage',
    );
    await setView('ad_products');
    await wait("document.querySelectorAll('.promotions-panel tbody tr').length>=6");
    await assert(
      "!!document.querySelector('.promotions-panel .goods-with-thumbnail img')&&document.querySelector('.promotions-panel').textContent.includes('全店模式')",
      'Promotion product thumbnails and full-shop paused plan',
    );
    await capture('taobao-promotions-final.png');
    await setView('ad_reports');
    await wait("document.querySelector('.promotions-panel tbody')?.textContent.includes('关键词推广')");
    await assert(
      "document.querySelector('.promotions-panel').textContent.includes('1–2 小时')&&document.querySelector('.promotion-metrics strong').textContent!=='—'",
      'Report ROI and official real-time exclusions',
    );
    await capture('taobao-ad-reports-final.png');
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL Taobao UI checks passed');
  } finally {
    if (call) {
      await call('Browser.close').catch(() => {});
      ws.close();
    } else chrome.kill();
  }
})().catch((e) => {
  console.error(e.stack || e.message);
  process.exitCode = 1;
});
