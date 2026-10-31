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
      '--remote-debugging-port=19337',
      `--user-data-dir=${path.join(root, '.data/ui-workspace-updates')}`,
      'http://127.0.0.1:3210/',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19337/json/list')).json()).find((t) => t.type === 'page');
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
    await evaluate("localStorage.setItem('pdd-manager:theme','forest')");
    await view('settings');
    await wait("document.querySelectorAll('.theme-options button').length===3");
    await assert(
      "[...document.querySelectorAll('.theme-options button')].map(e=>e.textContent.replace(' · 当前','')).join(',')==='白色,暗色,蓝色'&&document.documentElement.dataset.theme==='white'",
      'Only white, dark, blue themes; legacy preference migrated',
    );
    await click('蓝色');
    await assert("localStorage.getItem('pdd-manager:theme')==='blue'", 'Theme selection persists');
    await capture('updates-theme-blue.png');
    await view('overview');
    await wait(
      "document.querySelector('.new-products-number strong')?.textContent!=='—'&&document.querySelectorAll('.shop-comparison-chart').length>0",
    );
    await assert(
      "document.querySelectorAll('.shop-comparison-chart polygon').length>0&&!!document.querySelector('.comparison-gap')",
      'Bright filled trend charts and difference values',
    );
    await evaluate("document.querySelector('.new-products-strip').scrollIntoView()");
    await capture('updates-overview-blue.png');
    await evaluate("document.querySelector('.new-products-strip button[aria-expanded]').click()");
    await wait("document.querySelectorAll('.new-products-shops button').length===20");
    const date = await evaluate('document.querySelector(\'[aria-label="上新统计日期"]\').value');
    await evaluate("document.querySelector('.new-products-number').click()");
    await wait("document.querySelector('.business-heading h2')?.textContent==='商品列表'");
    await assert(
      `[...document.querySelectorAll('.filter-panel input[type=date]')].every(e=>e.value===${JSON.stringify(date)})`,
      'New product details use the selected creation date',
    );
    await view('ad_products');
    await wait("!!document.querySelector('.workspace-controls .saved-filters')");
    await assert(
      "(()=>{const a=document.querySelector('.saved-filters').getBoundingClientRect(),b=document.querySelector('.export-action').getBoundingClientRect();return b.left-a.right<25&&Math.abs(a.top-b.top)<5})()",
      'Saved filters aligned right alongside export and table settings',
    );
    await capture('updates-toolbar.png');
    await view('shops');
    await wait("document.querySelectorAll('.shop-card').length===20");
    await assert(
      "!!document.querySelector('[aria-label=\"运营负责人筛选\"]')&&document.querySelector('.shop-card-meta').textContent.includes('运营：')",
      'Shop operator display and filtering',
    );
    await click('＋ 添加店铺');
    await wait("!!document.querySelector('.shop-editor')");
    await evaluate(
      "(()=>{const e=document.querySelector('[aria-label=\"店铺登录方式\"]');Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(e,'qr');e.dispatchEvent(new Event('change',{bubbles:true}));})()",
    );
    await assert(
      "!document.querySelector('.shop-editor input[type=password]')&&!!document.querySelector('[aria-label=\"店铺运营负责人\"]')&&!document.querySelector('.shop-editor input[autocomplete=off]').required",
      'QR creation form requires neither password nor account',
    );
    await capture('updates-qr-add.png');
    await evaluate('document.querySelector(\'.shop-editor [aria-label="关闭"]\').click()');
    await view('settings');
    await wait("!!document.querySelector('.theme-options')");
    await click('暗色');
    await view('overview');
    await wait("document.querySelectorAll('.shop-comparison-chart').length>0");
    await evaluate("document.querySelector('.shop-comparisons').scrollIntoView()");
    await capture('updates-trends-dark.png');
    await view('settings');
    await wait("!!document.querySelector('.theme-options')");
    await click('白色');
    await view('overview');
    await wait("document.querySelectorAll('.shop-comparison-chart').length>0");
    await evaluate("document.querySelector('.shop-comparisons').scrollIntoView()");
    await capture('updates-trends-white.png');
    await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
    await pause(300);
    await assert('document.documentElement.scrollWidth<=innerWidth+1', 'New overview fits mobile width');
    await evaluate("document.querySelector('.new-products-strip').scrollIntoView()");
    await capture('updates-mobile.png');
    if (errors.length) throw Error('Browser errors: ' + errors.join(' | '));
    console.log('Workspace updates UI checks passed');
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
