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
      '--remote-debugging-port=19344',
      `--user-data-dir=${path.join(root, '.data/ui-compact-platforms')}`,
      'http://127.0.0.1:3210/',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19344/json/list')).json()).find((t) => t.type === 'page');
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
    await evaluate("localStorage.setItem('pdd-manager:platform','pdd')");
    await view('overview');
    await wait(
      "document.querySelectorAll('.shop-comparison').length>0&&document.querySelectorAll('.shop-comparison').length===document.querySelectorAll('[aria-label=\"概览店铺\"] option').length-1",
    );
    await assert(
      "(()=>{const g=document.querySelector('.shop-comparison-grid'),c=g.firstElementChild;return getComputedStyle(g).gridTemplateColumns.split(' ').length>=5&&c.getBoundingClientRect().height<=205})()",
      'All shop cards adapt to desktop width',
    );
    await assert(
      "!document.querySelector('.ov-source-details')&&document.querySelectorAll('.ov-store-section .workspace-controls .saved-filters,.ov-store-section .workspace-controls .export-action,.ov-store-section .workspace-controls .column-settings').length===3",
      'Saved filters and export placed beside table settings; old tools section removed',
    );
    await evaluate("document.querySelector('.shop-comparisons').scrollIntoView({block:'start'})");
    await capture('compact-trends.png');
    const box = await evaluate(
      "(()=>{const r=document.querySelector('.shop-comparison').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()",
    );
    await call('Input.dispatchMouseEvent', { type: 'mouseMoved', ...box });
    await pause(350);
    await assert(
      "getComputedStyle(document.querySelector('.shop-comparison')).transform.startsWith('matrix(1.42')",
      'Hovered card enlarges substantially without changing grid layout',
    );
    await capture('compact-trend-hover.png');
    await call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: 10, y: 10 });
    await pause(200);
    await evaluate("document.querySelector('.ov-store-section').scrollIntoView({block:'start'})");
    await capture('overview-table-toolbar.png');
    await assert(
      "!document.querySelector('[aria-label=\"切换工作平台\"]')&&!document.querySelector('.platform-home')",
      'Unified home has no global platform switch',
    );
    await click('拼多多专属');
    await wait("document.querySelector('.platform-feature-tag')?.textContent==='拼多多专属功能'");
    await click('数据中心');
    await wait("!!document.querySelector('.overview-dashboard')");
    await click('系统管理');
    await click('系统设置');
    await wait("document.querySelector('.settings-page')?.textContent.includes('拼多多 · 模块采集方式')");
    await assert(
      "document.body.innerText.includes('拼多多 · 模块采集方式')",
      'Shared settings explicitly identify PDD collection rules',
    );
    await evaluate("document.querySelector('.sidebar-brand').click()");
    await wait(
      "document.querySelectorAll('.shop-comparison').length>0&&document.querySelectorAll('.shop-comparison').length===document.querySelectorAll('[aria-label=\"概览店铺\"] option').length-1",
    );
    await evaluate(
      "document.documentElement.dataset.theme='dark';document.querySelector('.shop-comparisons').scrollIntoView({block:'start'})",
    );
    await capture('compact-dark.png');
    await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
    await pause(250);
    await assert(
      'document.documentElement.scrollWidth<=innerWidth+1',
      'Mobile platform switcher and compact charts fit screen',
    );
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL compact homepage and platform scope UI checks passed');
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
