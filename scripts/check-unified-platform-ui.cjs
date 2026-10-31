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
      '--remote-debugging-port=19346',
      `--user-data-dir=${path.join(root, '.data/ui-unified-platforms')}`,
      'http://127.0.0.1:3210/',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19346/json/list')).json()).find((t) => t.type === 'page');
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

    const requests = [];
    await call('Network.enable');
    ws.addEventListener('message', async (e) => {
      const m = JSON.parse(e.data);
      if (m.method === 'Network.requestWillBeSent') requests.push(m.params.request.url);
      if (m.method === 'Fetch.requestPaused') {
        const p = m.params;
        try {
          if (p.responseStatusCode === 200) {
            const body = await call('Fetch.getResponseBody', { requestId: p.requestId });
            const shops = JSON.parse(body.base64Encoded ? Buffer.from(body.body, 'base64').toString() : body.body);
            if (Array.isArray(shops)) {
              const fixture = {
                ...shops[0],
                id: 900001,
                name: '统一首页验证店铺',
                alias: '统一首页验证店铺',
                username: 'fixture-only',
                platform: 'jd',
                state: 'logged_in',
                operator: '测试运营',
              };
              await call('Fetch.fulfillRequest', {
                requestId: p.requestId,
                responseCode: 200,
                responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
                body: Buffer.from(JSON.stringify([...shops, fixture])).toString('base64'),
              });
              return;
            }
          }
          await call('Fetch.continueRequest', { requestId: p.requestId });
        } catch (error) {
          errors.push(error.message);
          await call('Fetch.continueRequest', { requestId: p.requestId }).catch(() => {});
        }
      }
    });
    await call('Fetch.enable', {
      patterns: [{ urlPattern: 'http://127.0.0.1:3210/api/shops', requestStage: 'Response' }],
    });
    await wait("!!document.querySelector('.workspace-sidebar')");
    await evaluate(
      "localStorage.setItem('pdd-manager:platform','jd');localStorage.setItem('pdd-manager:view','overview');localStorage.setItem('pdd-manager:auto-refresh','false');location.reload()",
    );
    await wait(
      "!!document.querySelector('[aria-label=\"概览店铺\"] option[value=\"900001\"]')&&document.querySelectorAll('.shop-comparison').length===document.querySelectorAll('[aria-label=\"概览店铺\"] option').length-1&&document.querySelectorAll('.ov-store-table tbody tr').length===document.querySelectorAll('.shop-comparison').length",
    );
    await assert(
      "!document.querySelector('[aria-label=\"切换工作平台\"]')&&!document.querySelector('.platform-home')",
      'Legacy platform selection no longer splits the home',
    );
    await assert(
      "document.querySelector('.overview-coverage-note').textContent.includes('业务数据待接入')&&[...document.querySelectorAll('.ov-store-table tbody tr')].find(r=>r.textContent.includes('统一首页验证店铺')).textContent.includes('业务数据待接入')",
      'All platforms appear in one overview with pending data clearly marked',
    );
    await wait("!!document.querySelector('.shop-comparison-chart')");
    await evaluate("document.querySelector('.shop-comparisons').scrollIntoView({block:'start'})");
    await capture('unified-home-mixed-platform.png');
    await evaluate(
      "(()=>{const e=document.querySelector('[aria-label=\"概览店铺\"]');e.value='900001';e.dispatchEvent(new Event('change',{bubbles:true}));})()",
    );
    await pause(1100);
    requests.length = 0;
    await pause(1100);
    await assert(
      "document.querySelector('.ov-revenue-value').textContent==='—'&&document.querySelector('.shop-comparison-empty').textContent==='业务数据待接入'&&document.querySelector('.arrival-comparison strong').textContent.startsWith('—')",
      'Unconnected store does not reuse revenue, trend or product totals',
    );
    if (requests.some((url) => url.includes('/api/data/')))
      throw Error('Unsupported selection still requests PDD business data');
    console.log('PASS unsupported store stops homepage business requests');
    await evaluate('document.querySelector(\'[aria-label="查看京东 · 统一首页验证店铺交易数据"]\').click()');
    await wait("!!document.querySelector('.pending-business')");
    await assert(
      "document.querySelector('.pending-business h2').textContent==='交易数据'&&document.querySelector('[aria-label=\"业务平台筛选\"]').value==='jd'",
      'Store drilldown keeps common feature and routes platform locally',
    );
    await evaluate("document.querySelector('.sidebar-brand').click()");
    await wait(
      "document.querySelectorAll('.shop-comparison').length>1&&document.querySelectorAll('.shop-comparison').length===document.querySelectorAll('[aria-label=\"概览店铺\"] option').length-1",
    );
    await click('商品评价');
    await wait('!!document.querySelector(\'[aria-label="业务平台筛选"]\')');
    await evaluate(
      "(()=>{const e=document.querySelector('[aria-label=\"业务平台筛选\"]');e.value='taobao';e.dispatchEvent(new Event('change',{bubbles:true}));})()",
    );
    await wait("!!document.querySelector('.pending-business')");
    await assert(
      "document.querySelector('.pending-business h2').textContent==='商品列表'",
      'Common product page remains the same for another platform',
    );
    await click('拼多多专属');
    await wait("!!document.querySelector('.platform-feature-tag')");
    await assert(
      "document.querySelector('.platform-feature-tag').textContent==='拼多多专属功能'&&!document.querySelector('.pending-business')&&document.querySelector('.business-heading h2').textContent==='商品领航员'",
      'Only exclusive features use a platform-specific entry',
    );
    await click('品牌黑标权益');
    await wait("document.querySelector('.business-heading h2')?.textContent==='品牌黑标权益'");
    await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
    await pause(200);
    await assert(
      'document.documentElement.scrollWidth<=innerWidth+1',
      'Unified navigation and feature scope fit mobile',
    );
    await evaluate("document.querySelector('.sidebar-brand').click()");
    await wait("!!document.querySelector('.overview-dashboard')");
    await call('Page.reload');
    await wait(
      "document.querySelectorAll('.shop-comparison').length>1&&document.querySelectorAll('.shop-comparison').length===document.querySelectorAll('[aria-label=\"概览店铺\"] option').length-1",
    );
    await assert(
      "document.querySelector('[aria-label=\"概览店铺\"]').value===''",
      'Home returns to all shops after refresh',
    );
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL unified platform UI checks passed');
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
