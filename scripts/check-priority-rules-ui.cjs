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
      '--remote-debugging-port=19375',
      `--user-data-dir=${path.join(root, '.data/ui-attention-link')}`,
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19375/json/list')).json()).find((t) => t.type === 'page');
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

    let version = 'A',
      reads = 0,
      commands = 0;
    const fixture = () => ({
      rows: [
        {
          id: 'case1',
          created_at: '2026-09-01 10:00:00',
          shop_id: 1,
          shop_name: '验证店铺',
          platform: 'pdd',
          kind: 'aftersales',
          module_name: '售后待处理',
          order_number: '验证订单' + version,
          status: '平台介入待商家处理',
          priority: 3,
          updated_at: Date.now() / 1000,
        },
      ],
      total: version === 'A' ? 3 : 4,
      page: 1,
      page_count: 1,
      pending: 0,
      stale: 0,
      sources: [],
    });
    ws.addEventListener('message', async (e) => {
      const m = JSON.parse(e.data);
      if (m.method !== 'Fetch.requestPaused') return;
      const p = m.params;
      try {
        if (p.request.method === 'POST') commands++;
        else reads++;
        await call('Fetch.fulfillRequest', {
          requestId: p.requestId,
          responseCode: 200,
          responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
          body: Buffer.from(
            JSON.stringify(
              p.request.url.includes('/api/data/aftersales')
                ? {
                    ...fixture(),
                    total: 1,
                    priority_count: 1,
                    statuses: ['平台介入待商家处理'],
                    activity_types: [],
                    metrics: [],
                    scope: {},
                    sources: [],
                  }
                : fixture(),
            ),
          ).toString('base64'),
        });
      } catch (e) {
        errors.push(e.message);
      }
    });
    await call('Runtime.enable');
    await call('Page.enable');
    await call('Fetch.enable', {
      patterns: [{ urlPattern: '*/api/attention*' }, { urlPattern: '*/api/data/aftersales*' }],
    });
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source: `if(location.origin==='http://127.0.0.1:3210'){localStorage.clear();sessionStorage.clear();localStorage.setItem('pdd-manager:view','overview');localStorage.setItem('pdd-manager:auto-refresh','false');window.__stream=null;window.EventSource=class{static OPEN=1;constructor(){this.readyState=1;this.listeners={};window.__stream=this;}addEventListener(name,fn){this.listeners[name]=fn;}close(){this.readyState=2;}emit(version){this.listeners.database?.({data:JSON.stringify({version})});}};}`,
    });
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait("document.querySelector('.ov-attention-link strong')?.textContent==='3'");
    await assert(
      "!!document.querySelector('.ov-attention .ov-attention-link')&&!document.querySelector('.overview-dashboard .attention-panel')",
      'home shows count inside action center without detail list',
    );
    version = 'B';
    await evaluate("window.__stream.emit('test:2')");
    await wait("document.querySelector('.ov-attention-link strong')?.textContent==='4'");
    if (commands) throw Error('Count refresh submitted a collection command');
    console.log('PASS pushed count update');
    await evaluate("document.querySelector('.ov-attention-link').click()");
    await wait("document.querySelector('.business-heading h2')?.textContent==='重点待办中心'");
    await wait("document.querySelector('.attention-panel')?.innerText.includes('验证订单B')");
    console.log('PASS count entry opens dedicated task page');
    await evaluate("localStorage.setItem('pdd-manager:view','overview')");
    await call('Page.reload');
    await wait("document.querySelector('.ov-attention-link strong')?.textContent==='4'");
    await wait("document.querySelector('select[aria-label=概览店铺]')?.options.length>1");
    const shopId = await evaluate(
      "(()=>{const s=document.querySelector('select[aria-label=概览店铺]');return Array.from(s.options).find(o=>o.value)?.value;})()",
    );
    if (!shopId) throw Error('No shop available for navigation check');
    await evaluate(
      `(()=>{const s=document.querySelector('select[aria-label=概览店铺]');Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(s,${JSON.stringify(shopId)});s.dispatchEvent(new Event('change',{bubbles:true}));})()`,
    );
    await pause(800);
    await evaluate("document.querySelector('.ov-attention-link').scrollIntoView({block:'center'})");
    const card = await call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(root, '.data/attention-link-home.png'), Buffer.from(card.data, 'base64'));
    await evaluate("document.querySelector('.ov-attention-link').click()");
    await wait("document.querySelector('.business-heading h2')?.textContent==='重点待办中心'");
    await wait(
      `Array.from(document.querySelectorAll('.filter-panel label')).find(l=>l.textContent.startsWith('店铺'))?.querySelector('select')?.value===${JSON.stringify(shopId)}`,
    );
    console.log('PASS shop scope preserved after navigation');
    const screenshot = await call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(root, '.data/attention-link-ui.png'), Buffer.from(screenshot.data, 'base64'));
    await assert(
      "document.querySelectorAll('.attention-panel').length===1&&document.querySelectorAll('.business-panel table').length===1",
      'one centralized priority list',
    );
    await assert(
      "Array.from(document.querySelectorAll('.filter-panel label')).find(l=>l.textContent.startsWith('事项类型')).querySelector('select').options.length===7",
      'six focus module filters',
    );
    for (const theme of ['white', 'dark', 'blue']) {
      await evaluate(`document.documentElement.dataset.theme='${theme}'`);
      await pause(600);
      console.log(
        theme,
        await evaluate(
          "JSON.stringify({theme:document.documentElement.dataset.theme,bg:getComputedStyle(document.querySelector('.attention-row td')).backgroundColor})",
        ),
      );
      await assert(
        `getComputedStyle(document.querySelector('.attention-row td')).backgroundColor==='${theme === 'dark' ? 'rgb(55, 32, 41)' : 'rgb(255, 241, 242)'}'`,
        'red row background in ' + theme,
      );
    }
    await evaluate(
      "Array.from(document.querySelectorAll('.attention-panel button')).find(b=>b.textContent==='查看模块').click()",
    );
    await wait("document.querySelector('.business-heading h2')?.textContent==='售后工作台'");
    await wait("!!document.querySelector('.business-panel .attention-row')");
    await assert(
      "Array.from(document.querySelectorAll('.filter-panel input[type=date]')).every(e=>e.value==='2026-09-01')",
      'old focus order keeps its date when opening module',
    );
    await assert(
      "!document.querySelector('.business-panel .attention-panel')",
      'submodule contains only its own table',
    );
    await assert(
      "getComputedStyle(document.querySelector('.business-panel .attention-row td')).backgroundColor==='rgb(255, 241, 242)'",
      'priority row red in submodule',
    );
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL ATTENTION UI CHECKS PASSED');
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
