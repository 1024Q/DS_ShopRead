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
      '--remote-debugging-port=19330',
      `--user-data-dir=${path.join(root, '.data/ui-density-test')}`,
      'http://127.0.0.1:3210/',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19330/json/list')).json()).find((t) => t.type === 'page');
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
    const view = async (value) => {
      await evaluate(
        `localStorage.setItem('pdd-manager:auto-refresh','false');localStorage.setItem('pdd-manager:view',${JSON.stringify(value)});location.reload()`,
      );
    };
    const set = async (selector, value) => {
      await evaluate(
        `(()=>{const e=document.querySelector(${JSON.stringify(selector)});Object.getOwnPropertyDescriptor(e.tagName==='SELECT'?HTMLSelectElement.prototype:HTMLInputElement.prototype,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event(e.tagName==='SELECT'?'change':'input',{bubbles:true}));})()`,
      );
      await pause(500);
    };
    await view('sources');
    await wait("document.querySelectorAll('.sync-table tbody tr').length>0");
    await assert(
      "document.querySelectorAll('.sync-table tbody tr').length<=30&&!document.querySelector('.source-list')",
      'Sources use bounded pagination instead of hundreds of cards',
    );
    await click('下一页');
    await assert("document.querySelector('.pagination').textContent.includes('第 2 /')", 'Source pagination works');
    await set('[aria-label="数据源搜索"]', 'density-no-match-fixture');
    await assert(
      "document.querySelectorAll('.sync-table tbody tr').length===0&&document.querySelector('.pagination').textContent.includes('第 1 /')",
      'Search filters and resets pagination',
    );
    await set('[aria-label="数据源搜索"]', '');
    await capture('density-sources.png');
    await view('logs');
    await wait("document.querySelectorAll('.log-table tbody tr').length>0");
    await assert("getComputedStyle(document.querySelector('.log-table td')).fontSize==='12px'", 'Log font is compact');
    await capture('density-logs.png');
    await view('products');
    await wait(
      "document.querySelectorAll('.data-table tbody tr').length>0&&!!document.querySelector('.column-settings')",
    );
    await assert(
      "getComputedStyle(document.querySelector('.data-table td')).fontSize==='13px'",
      'Business table font is 13px',
    );
    await assert(
      "(()=>{const items=[...document.querySelectorAll('.workspace-controls>.tool-popover,.workspace-controls>.export-action')];return items.length===3&&Math.max(...items.map(e=>e.getBoundingClientRect().top))-Math.min(...items.map(e=>e.getBoundingClientRect().top))<3;})()",
      'Saved filters, export and table settings share one row',
    );
    await evaluate(
      "window.__tableTop=document.querySelector('.data-table-wrap').getBoundingClientRect().top;document.querySelector('.column-settings>summary').click()",
    );
    await pause(200);
    await assert(
      "document.querySelector('.column-settings').open&&document.querySelector('.data-table-wrap').getBoundingClientRect().top===window.__tableTop",
      'Table settings opens without moving the table',
    );
    await capture('density-table-settings.png');
    await evaluate("document.querySelector('.column-checkboxes input:not(:disabled)').click()");
    await pause(200);
    await assert("!!document.querySelector('.data-table th[hidden]')", 'Column toggles still work');
    await click('恢复默认列');
    await call('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape' });
    await assert("!document.querySelector('.column-settings').open", 'Escape dismisses table settings');
    await evaluate("document.querySelector('.saved-filters>summary').click()");
    await set('[aria-label="筛选方案名称"]', 'density-fixture');
    await click('保存当前筛选');
    await assert(
      "[...document.querySelectorAll('[aria-label=常用筛选] option')].some(e=>e.value==='density-fixture')",
      'Saved filter creation works in compact popover',
    );
    await click('删除方案');
    await evaluate("document.documentElement.dataset.theme='dark'");
    await capture('density-table-dark.png');
    await assert(
      "getComputedStyle(document.querySelector('.saved-filters .tool-popover-panel')).backgroundColor!=='rgb(255, 255, 255)'",
      'Tool popovers honor dark theme',
    );
    await evaluate(
      "document.querySelector('.saved-filters').open=false;document.documentElement.dataset.theme='forest'",
    );
    await capture('density-products.png');
    await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
    await pause(300);
    await assert('document.documentElement.scrollWidth<=innerWidth+1', 'Table page fits mobile viewport');
    await evaluate("document.querySelector('.column-settings>summary').click()");
    await assert(
      "(()=>{const b=document.querySelector('.column-settings .tool-popover-panel').getBoundingClientRect();return b.left>=0&&b.right<=innerWidth;})()",
      'Settings popover stays in mobile viewport',
    );
    await capture('density-mobile.png');
    if (errors.length) throw Error(errors.join('\n'));
    console.log('Density UI checks passed');
  } finally {
    if (call) {
      await call('Browser.close').catch(() => {});
      ws.close();
    } else chrome.kill();
  }
})().catch((error) => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
