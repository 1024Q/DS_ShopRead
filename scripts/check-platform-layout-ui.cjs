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
      '--remote-debugging-port=19373',
      `--user-data-dir=${path.join(root, '.data/ui-platform-layout')}`,
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19373/json/list')).json()).find((t) => t.type === 'page');
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

    await call('Runtime.enable');
    await call('Page.enable');
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source:
        "if(location.origin==='http://127.0.0.1:3210'){localStorage.clear();sessionStorage.clear();localStorage.setItem('pdd-manager:view','goodsdata');localStorage.setItem('pdd-manager:auto-refresh','false');}",
    });
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait(
      "document.querySelector('.goods-analytics .taobao-traffic')&&document.querySelectorAll('.goods-analytics tbody tr').length>2",
    );
    await assert(
      "document.querySelector('.goods-analytics').firstElementChild.innerText.startsWith('商品概况')",
      'all-shop overview first',
    );
    await assert(
      "document.querySelector('.goods-analytics').firstElementChild.innerText.includes('拼多多')&&document.querySelector('.goods-analytics').firstElementChild.innerText.includes('淘宝')",
      'overview includes both platforms',
    );
    await assert(
      "!!(document.querySelector('.goods-analytics .pagination').compareDocumentPosition(document.querySelector('.taobao-traffic')) & Node.DOCUMENT_POSITION_FOLLOWING)",
      'Taobao traffic follows common product details',
    );
    const platform = async (value) => {
      await evaluate(
        `(()=>{const el=document.querySelector('select[aria-label="业务平台筛选"]');el.value=${JSON.stringify(value)};el.dispatchEvent(new Event('change',{bubbles:true}));})()`,
      );
    };
    await platform('taobao');
    await wait("document.querySelector('.goods-analytics')?.firstElementChild.classList.contains('taobao-traffic')");
    console.log('PASS selected Taobao promotes traffic');
    await platform('');
    await wait("document.querySelector('.goods-analytics')?.firstElementChild.innerText.startsWith('商品概况')");
    await evaluate(
      "(()=>{const el=[...document.querySelectorAll('.filter-panel select')].find(e=>e.options[0]?.text==='全部店铺');el.value=[...el.options].find(o=>o.text.startsWith('淘宝')).value;el.dispatchEvent(new Event('change',{bubbles:true}));})()",
    );
    await wait("document.querySelector('.goods-analytics')?.firstElementChild.classList.contains('taobao-traffic')");
    console.log('PASS selected Taobao shop promotes traffic');
    await evaluate("[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='交易数据').click()");
    await wait("document.querySelector('.transaction-data')?.firstElementChild.innerText.startsWith('交易概况')");
    await wait("document.querySelector('.transaction-data')?.lastElementChild.innerText.includes('拼多多 · 订单数据')");
    console.log('PASS all-platform transaction keeps PDD module below');
    await platform('pdd');
    await wait(
      "document.querySelector('.transaction-data')?.firstElementChild.innerText.startsWith('拼多多 · 订单数据')",
    );
    console.log('PASS selected PDD promotes order module');
    await platform('taobao');
    await wait(
      "document.querySelector('.transaction-data')?.firstElementChild.innerText.startsWith('交易概况')&&!document.querySelector('.transaction-data').innerText.includes('拼多多 · 订单数据')",
    );
    console.log('PASS Taobao excludes PDD order module');
    await evaluate(
      "(()=>{const button=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='推广报表');if(button)button.click();else [...document.querySelectorAll('button')].find(e=>e.textContent.includes('推广平台')).click();})()",
    );
    await evaluate("[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='推广报表').click()");
    await wait("document.querySelector('.promotions-panel')?.innerText.includes('推广概况')");
    console.log('PASS promotion overview renders');
    const screenshot = await call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(root, '.data/platform-layout-ui.png'), Buffer.from(screenshot.data, 'base64'));
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL GOODS LAYOUT CHECKS PASSED');
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
