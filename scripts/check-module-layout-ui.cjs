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
      '--remote-debugging-port=19358',
      `--user-data-dir=${path.join(root, '.data/ui-module-layout')}`,
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19358/json/list')).json()).find((t) => t.type === 'page');
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

    const syncCommands = [];
    ws.addEventListener('message', async (e) => {
      const m = JSON.parse(e.data);
      if (m.method === 'Fetch.requestPaused') {
        const p = m.params;
        try {
          syncCommands.push(p.request.url);
          await call('Fetch.fulfillRequest', {
            requestId: p.requestId,
            responseCode: 202,
            responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
            body: Buffer.from('{}').toString('base64'),
          });
        } catch (e) {
          if (!/Invalid InterceptionId/i.test(e.message)) errors.push(e.message);
        }
      }
    });
    await call('Runtime.enable');
    await call('Page.enable');
    await call('Fetch.enable', { patterns: [{ urlPattern: '*/api/data/*/sync*' }] });
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source:
        "if(location.origin==='http://127.0.0.1:3210'&&!sessionStorage.headerTools){localStorage.setItem('pdd-manager:view','products');localStorage.setItem('pdd-manager:filters:products',JSON.stringify({draft:{},pageSize:20}));localStorage.setItem('pdd-manager:auto-refresh','false');sessionStorage.headerTools='1';}",
    });
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait('!!document.querySelector(\'.filter-panel [aria-label="业务平台筛选"]\')', 60);
    await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent==='关闭画面')?.click()");
    await assert(
      "!!document.querySelector('header .global-sync-bar')&&!document.querySelector('.page-content .business-scope')&&document.querySelectorAll('.global-sync-bar').length===1",
      'Synchronization remains in the top header',
    );
    await assert(
      "![...document.querySelectorAll('header button')].some(b=>b.textContent==='系统设置')",
      'Top settings button removed',
    );
    await assert(
      "!!document.querySelector('header [aria-label=\"工作机器\"]')&&document.querySelector('header').getBoundingClientRect().height<=110",
      'Desktop header preserves machine selection in a compact layout',
    );
    await wait("!!document.querySelector('.business-panel tbody tr')", 45);
    await assert(
      "(()=>{const p=document.querySelector('.platform-filter'),s=p.nextElementSibling;return s?.textContent.startsWith('店铺')&&p.getBoundingClientRect().right<=s.getBoundingClientRect().left+1;})()",
      'Platform filter is immediately left of shop filter',
    );
    const before = syncCommands.length;
    await evaluate("[...document.querySelectorAll('header button')].find(b=>b.textContent==='立即同步').click()");
    await pause(600);
    if (syncCommands.slice(before).every((url) => !url.includes('force=true')))
      throw Error('Refresh action did not reach the active panel');
    console.log('PASS top refresh still dispatches current-page sync (intercepted for test)');
    await evaluate(
      "(()=>{const e=document.querySelector('[aria-label=\"业务平台筛选\"]');e.value='jd';e.dispatchEvent(new Event('change',{bubbles:true}));})()",
    );
    await wait("!!document.querySelector('.pending-business')");
    await assert(
      "document.querySelector('.pending-business').textContent.includes('京东')",
      'Platform selector preserves page routing',
    );
    await evaluate(
      "(()=>{const e=document.querySelector('[aria-label=\"业务平台筛选\"]');e.value='pdd';e.dispatchEvent(new Event('change',{bubbles:true}));})()",
    );
    await wait("!!document.querySelector('.business-panel tbody tr')");
    const shot = async (name) => {
      await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent==='关闭画面')?.click()");
      await pause(200);
      fs.writeFileSync(
        path.join(root, '.data', name),
        Buffer.from((await call('Page.captureScreenshot', { format: 'png' })).data, 'base64'),
      );
    };
    await shot('module-layout-white.png');
    await evaluate("document.documentElement.dataset.theme='dark'");
    await shot('module-layout-dark.png');
    for (const width of [1280, 900, 390]) {
      await call('Emulation.setDeviceMetricsOverride', { width, height: 1000, deviceScaleFactor: 1, mobile: false });
      await pause(250);
      await assert(
        "(()=>{const h=document.querySelector('header'),r=h.getBoundingClientRect();return r.width<=innerWidth&&h.scrollWidth<=h.clientWidth+1&&[...h.querySelectorAll('select,button')].filter(e=>e.getBoundingClientRect().width).every(e=>{const b=e.getBoundingClientRect();return b.left>=r.left-1&&b.right<=r.right+1;});})()",
        'Header controls fit viewport ' + width,
      );
    }
    await shot('module-layout-mobile.png');
    await call('Emulation.clearDeviceMetricsOverride');
    await evaluate("document.querySelector('.home-brand').click()");
    await wait("!!document.querySelector('.overview-dashboard')");
    await assert(
      "!!document.querySelector('header .global-sync-bar')&&!document.querySelector('.business-scope')",
      'Home remains unified across platforms',
    );
    await assert(
      "document.querySelector('[aria-label=\"业务平台筛选\"]').value===''",
      'Home defaults to all platforms',
    );
    await wait("document.querySelectorAll('.module-collapse').length>=5");
    await assert(
      "[...document.querySelectorAll('.module-collapse')].every(b=>b.getAttribute('aria-expanded')==='true')",
      'Home modules default to expanded',
    );
    await evaluate("document.querySelector('.shop-comparisons .module-collapse').click()");
    await assert(
      "document.querySelector('.shop-comparisons>.collapsible-body').hidden&&document.querySelector('.shop-comparisons .ov-chart-legend')!==null",
      'Collapse hides content while preserving the mounted charts',
    );
    await pause(5500);
    await assert(
      "document.querySelector('.shop-comparisons .module-collapse').getAttribute('aria-expanded')==='false'",
      'Live status refresh preserves collapsed state',
    );
    await evaluate("document.querySelector('.shop-comparisons .module-collapse').click()");
    await shot('module-layout-overview.png');
    await evaluate('document.querySelector(\'.workspace-sidebar [aria-label="系统管理"]\').click()');
    await assert(
      "[...document.querySelectorAll('.workspace-sidebar button')].some(b=>b.textContent==='系统设置')",
      'Sidebar settings entry retained',
    );
    await evaluate(
      "[...document.querySelectorAll('.workspace-sidebar button')].find(b=>b.textContent==='系统设置').click()",
    );
    await wait('!!document.querySelector(\'[aria-label="自定义采集间隔"]\')');
    await assert(
      "[...document.querySelectorAll('.module-collapse')].every(b=>b.getAttribute('aria-expanded')==='true')",
      'Settings modules default to expanded',
    );
    const setting = await evaluate('document.querySelector(\'[aria-label="自定义采集间隔"]\').value');
    await evaluate('document.querySelector(\'[aria-label="收起后台采集计划"]\').click()');
    await assert(
      "document.querySelector('[aria-label=\"自定义采集间隔\"]').closest('.collapsible-body').hidden",
      'Collection settings collapse',
    );
    await evaluate('document.querySelector(\'[aria-label="展开后台采集计划"]\').click()');
    if (setting !== (await evaluate('document.querySelector(\'[aria-label="自定义采集间隔"]\').value')))
      throw Error('Setting lost');
    await shot('module-layout-settings.png');
    for (const [view, selector] of [
      ['transactions', '.transaction-data'],
      ['goodsdata', '.goods-analytics'],
      ['tax', '.tax-subjects'],
      ['ad_reports', '.promotions-panel'],
    ]) {
      await evaluate(`localStorage.setItem('pdd-manager:view',${JSON.stringify(view)})`);
      await call('Page.reload');
      await wait(`!!document.querySelector(${JSON.stringify(selector)})`, 60);
      await wait("document.querySelectorAll('.module-collapse').length>0");
      await assert(
        "[...document.querySelectorAll('.module-collapse')].every(b=>b.getAttribute('aria-expanded')==='true')",
        view + ' defaults to expanded',
      );
      await evaluate("document.querySelector('.module-collapse').click()");
      await assert(
        "document.querySelector('.collapsible-section>.collapsible-body').hidden",
        view + ' module collapses',
      );
      await evaluate("document.querySelector('.module-collapse').click()");
    }
    await evaluate("document.documentElement.dataset.theme='blue'");
    await shot('module-layout-blue.png');
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL login/layout UI checks passed');
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
