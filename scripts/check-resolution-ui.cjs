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
      '--remote-debugging-port=19384',
      `--user-data-dir=${path.join(root, '.data/ui-focus-resolution')}`,
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19384/json/list')).json()).find((t) => t.type === 'page');
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

    let phase = 'pending';
    const source = {
      shop_id: 1,
      platform: 'pdd',
      shop_name: '测试店铺',
      complete: true,
      updated_at: new Date().toISOString(),
      loaded_count: 1,
    };
    ws.addEventListener('message', async (e) => {
      const m = JSON.parse(e.data);
      if (m.method !== 'Fetch.requestPaused') return;
      try {
        const p = m.params,
          rows =
            phase === 'removed'
              ? []
              : [
                  {
                    ...source,
                    id: 'case-one',
                    order_number: 'order-one',
                    name: '售后商品',
                    status: phase === 'pending' ? '平台处理中' : '退款成功',
                    priority: 0,
                    focus_pending: phase === 'pending',
                    amount: 10,
                    created_at: '2026-10-11 10:00:00',
                  },
                ];
        const data =
          p.request.method === 'POST'
            ? { tasks: [] }
            : {
                rows,
                total: rows.length,
                page_count: 1,
                statuses: ['平台处理中', '退款成功'],
                sources: [source],
                metrics: [],
                totals: {},
              };
        await call('Fetch.fulfillRequest', {
          requestId: p.requestId,
          responseCode: 200,
          responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
          body: Buffer.from(JSON.stringify(data)).toString('base64'),
        });
      } catch (e) {
        errors.push(e.message);
      }
    });
    await call('Runtime.enable');
    await call('Page.enable');
    await call('Fetch.enable', { patterns: [{ urlPattern: '*/api/data/*' }] });
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source: `if(location.origin==='http://127.0.0.1:3210'){localStorage.clear();sessionStorage.clear();localStorage.setItem('pdd-manager:view','aftersales');localStorage.setItem('pdd-manager:auto-refresh','true');}`,
    });
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait("document.querySelector('.focus-resolution-pending')", 60);
    await assert(
      "!document.querySelector('.data-table .attention-row')",
      'resolved membership no longer highlights the row',
    );
    await evaluate("[...document.querySelectorAll('.data-table button')].find(b=>b.textContent==='详情').click()");
    await wait("document.querySelector('[aria-label=数据详情]')");
    phase = 'verified';
    await evaluate(
      "window.dispatchEvent(new CustomEvent('database-updated',{detail:{changes:[{kind:'aftersales',shop_id:1}]}}))",
    );
    await wait("document.querySelector('.data-table tbody').textContent.includes('退款成功')");
    await assert("!document.querySelector('.focus-resolution-pending')", 'verification clears pending marker');
    await assert(
      "document.querySelector('[aria-label=数据详情]').textContent.includes('退款成功')",
      'open detail receives the new status',
    );
    phase = 'removed';
    await evaluate(
      "window.dispatchEvent(new CustomEvent('database-updated',{detail:{changes:[{kind:'aftersales',shop_id:1}]}}))",
    );
    await wait("!document.querySelector('[aria-label=数据详情]')");
    await assert(
      "!document.querySelector('.data-table tbody tr')",
      'record leaving current filter does not retain stale detail',
    );
    if (errors.length) throw Error(JSON.stringify(errors));
    console.log('RESOLUTION UI CHECKS PASSED');
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
