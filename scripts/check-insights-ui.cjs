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
      '--remote-debugging-port=19379',
      `--user-data-dir=${path.join(root, '.data/ui-insights')}`,
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19379/json/list')).json()).find((t) => t.type === 'page');
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
      if (!(await evaluate(expression))) throw Error(label);
      console.log('PASS', label);
    };

    let notify = 0;
    const commands = [];
    const health = {
      rows: [
        {
          shop_id: 5,
          shop_label: '拼多多 · 测试店铺',
          state: 'logged_in',
          runs: 10,
          success_rate: 90,
          average_seconds: 2,
          failed_modules: 1,
          consecutive: 1,
          browser_status: '最近采集正常',
        },
      ],
    };
    ws.addEventListener('message', async (e) => {
      const m = JSON.parse(e.data);
      if (m.method !== 'Fetch.requestPaused') return;
      const p = m.params,
        url = new URL(p.request.url);
      let value;
      if (url.pathname === '/api/attention/changes') {
        const after = url.searchParams.get('after');
        value = {
          machine: 'ui-test-insights',
          latest: notify ? 2 : 1,
          cursor: notify ? 2 : 1,
          rows:
            after === '1' && notify ? [{ id: 2, shop_id: 5, kind: 'evidence', record_id: 'test', change: 'new' }] : [],
        };
      } else if (url.pathname === '/api/system/health') value = health;
      else if (url.pathname === '/api/system/priority') {
        commands.push(p.request.url);
        value = { priority_until: Date.now() / 1000 + 900 };
      } else if (url.pathname === '/api/system/backup-verify') {
        commands.push(p.request.url);
        value = {
          verified: true,
          counts: { shops: 22, tax_records: 33, daily_archive: 44, collection_tasks: 55 },
          live_database_unchanged: true,
        };
      } else value = {};
      try {
        await call('Fetch.fulfillRequest', {
          requestId: p.requestId,
          responseCode: 200,
          responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
          body: Buffer.from(JSON.stringify(value)).toString('base64'),
        });
      } catch (e) {
        errors.push(e.message);
      }
    });
    await call('Runtime.enable');
    await call('Page.enable');
    await call('Fetch.enable', {
      patterns: [
        '*/api/attention/changes*',
        '*/api/system/health*',
        '*/api/system/priority*',
        '*/api/system/backup-verify*',
      ].map((urlPattern) => ({ urlPattern })),
    });
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source: `if(location.origin==='http://127.0.0.1:3210'){localStorage.clear();sessionStorage.clear();localStorage.setItem('pdd-manager:view','overview');localStorage.setItem('pdd-manager:auto-refresh','false');}`,
    });
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait("document.querySelector('.history-card')&&document.querySelector('.attention-notice')");
    await assert(
      "document.querySelector('.history-card').textContent.includes('历史对比')",
      'history component mounted',
    );
    await evaluate(
      "(()=>{const el=document.querySelector('[aria-label=历史对比周期]');el.value='30';el.dispatchEvent(new Event('change',{bubbles:true}));})()",
    );
    await wait("document.querySelector('.history-card').textContent.includes('近 30 天')");
    await assert("!document.querySelector('.attention-notice b')", 'initial import is quiet');
    notify = 1;
    await evaluate("document.dispatchEvent(new Event('visibilitychange'))");
    await wait("document.querySelector('.attention-notice b')?.textContent==='1'");
    await pause(5500);
    await assert(
      "document.querySelector('.attention-notice b').textContent==='1'",
      'unchanged event delivery does not repeat badge',
    );
    await evaluate("document.querySelector('.home-refresh-heading>button').click()");
    await wait("document.querySelector('.insights-health')?.textContent.includes('90%')");
    await assert(
      "document.querySelector('.insights-health').textContent.includes('拼多多 · 测试店铺')",
      'health shows actual sample denominator and platform identity',
    );
    await evaluate("Array.from(document.querySelectorAll('button')).find(x=>x.textContent==='查看任务明细')?.click()");
    await wait("document.querySelector('.queue-diagnostics')?.textContent.includes('临时优先')");
    const boost = await evaluate(
      "(()=>{const b=Array.from(document.querySelectorAll('.queue-diagnostics button')).find(x=>x.textContent==='优先 15 分钟');if(b)b.click();return !!b;})()",
    );
    if (boost) {
      await wait("document.querySelector('.queue-diagnostics').textContent.includes('未来 15 分钟')");
      if (!commands.some((x) => x.includes('/priority'))) throw Error('priority not submitted');
    }
    for (const theme of ['white', 'dark', 'blue']) {
      await evaluate(`document.documentElement.dataset.theme='${theme}'`);
      await pause(200);
      const shot = await call('Page.captureScreenshot', { format: 'png' });
      fs.writeFileSync(path.join(root, `.data/insights-${theme}.png`), Buffer.from(shot.data, 'base64'));
    }
    await evaluate("document.querySelector('.attention-notice button').click()");
    await wait("document.body.textContent.includes('重点待办')");
    await assert("!document.querySelector('.attention-notice b')", 'click clears badge and opens todo');
    await evaluate(
      "Array.from(document.querySelectorAll('.sidebar button,nav button')).find(x=>x.textContent.includes('系统设置'))?.click()",
    );
    // Also use the global schedule control, which opens settings regardless of navigation collapse state.
    await evaluate("document.querySelector('.global-auto-label').click()");
    await wait("document.querySelector('[aria-label=数据库备份]')");
    await wait("Array.from(document.querySelectorAll('button')).some(x=>x.textContent==='校验并预览')");
    await evaluate("Array.from(document.querySelectorAll('button')).find(x=>x.textContent==='校验并预览').click()");
    await wait("document.querySelector('.backup-preview')?.textContent.includes('当前数据库未被替换')");
    if (errors.length) throw Error(JSON.stringify(errors));
    console.log('ALL INSIGHTS UI CHECKS PASSED');
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
