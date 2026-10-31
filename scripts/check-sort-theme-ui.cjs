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
      '--remote-debugging-port=19343',
      `--user-data-dir=${path.join(root, '.data/ui-sort-theme-final')}`,
      'http://127.0.0.1:3210/',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19343/json/list')).json()).find((t) => t.type === 'page');
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
    await view('overview');
    await wait("document.querySelectorAll('.ov-store-table tbody tr').length===20");
    await evaluate("document.querySelector('.ov-store-table th:nth-child(2) .column-sort').click()");
    await pause(300);
    await assert(
      "(()=>{const rows=[...document.querySelectorAll('.ov-store-table tbody tr')].map(r=>r.cells[1].innerText).filter(v=>v!=='—').map(v=>Number(v.replace(/[¥,]/g,'')));return rows.length>0&&rows.every((v,i)=>!i||v>=rows[i-1])})()",
      'Client table sorts monetary cells correctly',
    );
    await evaluate("document.querySelector('.ov-store-table th:nth-child(2) .column-sort').click()");
    await pause(300);
    await assert(
      "(()=>{const rows=[...document.querySelectorAll('.ov-store-table tbody tr')].map(r=>r.cells[1].innerText).filter(v=>v!=='—').map(v=>Number(v.replace(/[¥,]/g,'')));return rows.every((v,i)=>!i||v<=rows[i-1])})()",
      'Client table descending preserves row identity',
    );
    await evaluate("document.documentElement.dataset.theme='dark';window.scrollTo(0,0)");
    await capture('v3-dark-final.png');
    await view('products');
    await wait("!!document.querySelector('.data-table tbody tr')");
    await evaluate("document.documentElement.dataset.theme='dark'");
    await capture('v3-dark-table-final.png');
    await assert(
      "getComputedStyle(document.querySelector('.data-table td')).color==='rgb(228, 236, 249)'",
      'Dark table uses theme text color',
    );
    await view('shops');
    await wait("document.querySelectorAll('.shop-card').length===20");
    await evaluate("document.documentElement.dataset.theme='dark'");
    await capture('v3-dark-shops-final.png');
    if (errors.length) throw Error(errors.join('\n'));
    console.log('PASS client sorting and final theme checks');
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
