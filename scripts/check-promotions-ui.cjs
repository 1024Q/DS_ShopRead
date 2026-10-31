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
      '--remote-debugging-port=19332',
      `--user-data-dir=${path.join(root, '.data/ui-promotions-test')}`,
      'http://127.0.0.1:3210/',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19332/json/list')).json()).find((t) => t.type === 'page');
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
      errors = [];
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
    await call('Runtime.enable');
    await call('Page.enable');
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    const evaluate = async (expression) => {
      const r = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
      return r.result.value;
    };
    const wait = async (expression, seconds = 60) => {
      for (let n = 0; n < seconds * 2; n++) {
        if (await evaluate(expression)) return;
        await pause(500);
      }
      console.log(await evaluate('document.body.innerText.slice(0,3500)'));
      throw Error('Timed out: ' + expression);
    };
    const click = async (text) => {
      await evaluate(
        `(()=>{const b=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()===${JSON.stringify(text)}||(e.matches('.nav-category')&&e.textContent.includes(${JSON.stringify(text)})));if(!b)throw Error('Missing button '+${JSON.stringify(text)});b.click();})()`,
      );
      await pause(250);
    };
    const capture = async (name) => {
      fs.writeFileSync(
        path.join(root, '.data', name),
        Buffer.from((await call('Page.captureScreenshot', { format: 'png' })).data, 'base64'),
      );
    };
    const assert = async (exp, label) => {
      if (!(await evaluate(exp))) throw Error(label);
      console.log('PASS', label);
    };

    await wait("!!document.querySelector('.workspace-sidebar')");
    const view = async (name) => {
      await evaluate(
        `localStorage.setItem('pdd-manager:auto-refresh','false');localStorage.setItem('pdd-manager:view',${JSON.stringify(name)});localStorage.removeItem('pdd-manager:filters:'+${JSON.stringify(name)});location.reload()`,
      );
      await pause(400);
    };
    await view('ad_products');
    await wait("document.querySelectorAll('.promotions-panel tbody tr').length>0");
    await assert(
      "document.querySelector('.business-heading h2').textContent==='商品推广'",
      'Product promotion navigation',
    );
    await assert(
      "document.querySelectorAll('.promotions-panel tbody tr').length===20&&document.querySelectorAll('.promotions-panel tbody img').length===20",
      'Paginated promotion products include thumbnails',
    );
    await assert(
      "[...document.querySelectorAll('.promotion-metrics strong')].every(e=>e.textContent!=='—')",
      'Promotion summary contains real metrics',
    );
    await capture('promotions-products.png');
    const select = async (label, value) => {
      await evaluate(
        `(()=>{const e=document.querySelector('[aria-label="'+${JSON.stringify(label)}+'"]');Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event('change',{bubbles:true}));})()`,
      );
    };
    await select('推广店铺', '1');
    await wait("document.querySelector('.table-caption strong')?.textContent.includes('45 条')");
    await evaluate(
      `(()=>{const e=document.querySelector('[aria-label="推广商品搜索"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,'unlikely-product-keyword-zz990');e.dispatchEvent(new Event('input',{bubbles:true}));})()`,
    );
    await wait("document.querySelector('.table-caption strong')?.textContent.includes('0 条')");
    console.log('PASS Shop and keyword filters automatically query');
    await view('ad_reports');
    await wait("document.querySelectorAll('.promotion-daily tbody tr').length>0");
    await assert(
      "document.querySelector('.business-heading h2').textContent==='推广报表'&&document.querySelectorAll('.promotion-daily tbody tr').length===20",
      'Reports include daily metrics for all 20 shops',
    );
    await capture('promotions-reports.png');
    await view('overview');
    await wait(
      "document.querySelectorAll('.shop-comparison').length===12&&document.querySelector('.overview-performance .promotion-metrics strong')?.textContent!=='—'",
    );
    await wait("document.querySelectorAll('.shop-comparison-chart').length>=10");
    await assert(
      "document.querySelectorAll('.shop-comparison-chart polyline').length>=20",
      'Per-shop today and yesterday curves',
    );
    await evaluate("document.querySelector('.overview-performance').scrollIntoView()");
    await capture('promotions-overview.png');
    await evaluate("document.querySelector('.shop-comparisons .pagination button:last-child').click()");
    await wait("document.querySelectorAll('.shop-comparison').length===8");
    console.log('PASS All 20 shop cards available through pagination');
    await evaluate(
      "[...document.querySelectorAll('[aria-label=\"多店铺走势指标\"] button')].find(e=>e.textContent==='订单数').click()",
    );
    await assert(
      "document.querySelector('[aria-label=\"多店铺走势指标\"] button[aria-pressed=true]').textContent==='订单数'&&!document.querySelector('.shop-comparison-values').textContent.includes('¥')",
      'Order trend metric changes',
    );
    await evaluate("document.documentElement.dataset.theme='dark'");
    await capture('promotions-overview-dark.png');
    await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
    await pause(300);
    await assert('document.documentElement.scrollWidth<=innerWidth+1', 'Overview responsive layout fits mobile');
    await evaluate("document.querySelector('.overview-performance').scrollIntoView()");
    await capture('promotions-mobile.png');
    await view('ad_products');
    await wait("document.querySelectorAll('.promotions-panel tbody tr').length>0");
    await assert(
      'document.documentElement.scrollWidth<=innerWidth+1',
      'Promotion table scrolls within mobile viewport',
    );
    if (errors.length) throw Error('Browser errors: ' + errors.join(' | '));
    console.log('Promotion UI checks passed');
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
