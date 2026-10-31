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

    let version = 0;
    const commands = [];
    const day = new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10),
      updated = day + 'T00:01:00+08:00';
    const fixture = () => ({
      date: day,
      updated_modules: version ? 2 : 1,
      total_modules: 2,
      updated_shops: version ? 2 : 1,
      total_shops: 2,
      last_updated: updated,
      rows: [
        {
          shop_id: 5,
          shop_name: '同名店铺',
          platform: 'pdd',
          status: 'updated',
          updated: 1,
          total: 1,
          last_updated: updated,
          modules: [
            { kind: 'overview', title: '经营概览', status: 'updated', today_updated: true, updated_at: updated },
          ],
        },
        {
          shop_id: 6,
          shop_name: '同名店铺',
          platform: 'taobao',
          status: version ? 'updated' : 'queued',
          updated: version ? 1 : 0,
          total: 1,
          last_updated: version ? updated : null,
          modules: [
            {
              kind: 'transactions',
              title: '交易 / 退款 / 走势',
              status: version ? 'updated' : 'queued',
              today_updated: !!version,
              updated_at: version ? updated : null,
            },
          ],
        },
      ],
    });
    ws.addEventListener('message', async (e) => {
      const m = JSON.parse(e.data);
      if (m.method !== 'Fetch.requestPaused') return;
      const p = m.params;
      try {
        if (p.request.method === 'POST') commands.push(p.request.url);
        await call('Fetch.fulfillRequest', {
          requestId: p.requestId,
          responseCode: 200,
          responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
          body: Buffer.from(JSON.stringify(fixture())).toString('base64'),
        });
      } catch (e) {
        errors.push(e.message);
      }
    });
    await call('Runtime.enable');
    await call('Page.enable');
    await call('Fetch.enable', { patterns: [{ urlPattern: '*/api/home/progress*' }] });
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source: `if(location.origin==='http://127.0.0.1:3210'){localStorage.clear();sessionStorage.clear();localStorage.setItem('pdd-manager:view','overview');localStorage.setItem('pdd-manager:auto-refresh','false');}`,
    });
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait("document.querySelector('.home-refresh-summary')?.textContent.includes('1 / 2 家')");
    await assert(
      "document.querySelector('.home-refresh-heading').textContent.includes('最后更新时间')",
      'last successful write timestamp shown',
    );
    await evaluate(
      "if(document.querySelector('.home-refresh-toggle').getAttribute('aria-expanded')==='false')document.querySelector('.home-refresh-toggle').click()",
    );
    await assert(
      "document.querySelectorAll('.home-refresh-shop').length===2&&document.querySelector('.home-refresh-shops').innerText.includes('拼多多')&&document.querySelector('.home-refresh-shops').innerText.includes('淘宝')",
      'shops identifiable by platform and individual state',
    );
    await evaluate("document.querySelectorAll('.home-refresh-shop summary')[1].click()");
    await assert(
      "document.querySelectorAll('.home-refresh-shop')[1].innerText.includes('交易 / 退款 / 走势')",
      'module detail available',
    );
    version = 1;
    await evaluate("document.dispatchEvent(new Event('visibilitychange'))");
    await wait("document.querySelector('.home-refresh-summary').textContent.includes('2 / 2 家')");
    await assert(
      "document.querySelector('.home-refresh-summary progress').value===2",
      'completion progress updates automatically',
    );
    const shot = await call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(root, '.data/home-refresh-progress-ui.png'), Buffer.from(shot.data, 'base64'));
    await evaluate("document.querySelector('.home-refresh-heading>button').click()");
    await wait("document.querySelector('.sync-center h2')?.textContent==='同步与数据源'");
    if (commands.length || errors.length)
      throw Error('Unexpected mutation or error: ' + JSON.stringify({ commands, errors }));
    console.log('ALL HOME PROGRESS UI CHECKS PASSED');
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
