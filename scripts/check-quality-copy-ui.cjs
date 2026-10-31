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
      '--remote-debugging-port=19382',
      `--user-data-dir=${path.join(root, '.data/ui-quality-copy')}`,
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19382/json/list')).json()).find((t) => t.type === 'page');
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
        if (await evaluate(`Boolean(${expression})`)) return;
        await pause(200);
      }
      throw Error('Timed out: ' + expression + '\n' + (await evaluate('document.body.innerText.slice(-2000)')));
    };
    const assert = async (expression, label) => {
      if (!(await evaluate(`Boolean(${expression})`))) throw Error(label);
      console.log('PASS', label);
    };

    await call('Runtime.enable');
    await call('Page.enable');
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source: `if(location.origin==='http://127.0.0.1:3210'){localStorage.clear();sessionStorage.clear();localStorage.setItem('pdd-manager:view','quality');localStorage.setItem('pdd-manager:auto-refresh','false');}`,
    });
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait("document.querySelector('.business-heading h2')?.textContent==='品质诊断'", 60);
    await wait("document.querySelector('.data-table tbody tr')", 60);
    await assert(
      "document.querySelector('.data-table').textContent.includes('生效状态')",
      'quality table shows effective status',
    );
    await assert(
      "!document.querySelector('.auto-query-note')&&!document.querySelector('.business-heading p')",
      'redundant page instructions removed',
    );
    await evaluate("document.querySelector('.priority-filter input').click()");
    await pause(2200);
    await assert(
      "[...document.querySelectorAll('.data-table tbody tr')].every(r=>!r.querySelector('.priority-label')||r.textContent.includes('生效中'))",
      'priority rows have effective status',
    );
    const shot = await call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(root, '.data/quality-copy.png'), Buffer.from(shot.data, 'base64'));
    await evaluate(
      "[...document.querySelectorAll('.nav-category')].find(x=>x.textContent.includes('数据中心')).click()",
    );
    await pause(1000);
    await evaluate("[...document.querySelectorAll('.main-tabs button')].find(x=>x.textContent==='商品数据').click()");
    await wait("document.querySelector('.business-heading h2')?.textContent==='商品数据'", 45);
    await pause(1800);
    await assert("document.querySelector('.context-note:not([open])')", 'statistics help starts collapsed');
    await evaluate("document.querySelector('.context-note summary').click()");
    await assert("document.querySelector('.context-note[open]')", 'statistics help can be expanded');
    await evaluate(
      "[...document.querySelectorAll('.nav-category')].find(x=>x.textContent.includes('系统管理')).click()",
    );
    await pause(700);
    await evaluate("[...document.querySelectorAll('.main-tabs button')].find(x=>x.textContent==='系统设置').click()");
    await wait("document.querySelector('[aria-label=采集策略平台]')", 30);
    await assert(
      "document.body.textContent.includes('重点事项每 60 秒检查')&&!document.body.textContent.includes('页面仅查询数据库；缺少数据时单独提交补采任务')",
      'collection settings are concise and preserve cadence',
    );
    if (errors.length) throw Error(JSON.stringify(errors));
    console.log('QUALITY AND COPY UI CHECKS PASSED');
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
