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
      '--remote-debugging-port=19347',
      `--user-data-dir=${path.join(root, '.data/ui-shop-identity')}`,
      'http://127.0.0.1:3210/',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19347/json/list')).json()).find((t) => t.type === 'page');
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
            let data = JSON.parse(body.base64Encoded ? Buffer.from(body.body, 'base64').toString() : body.body);
            if (new URL(p.request.url).pathname === '/api/shops')
              data = [
                ...data,
                ...[
                  ['pdd', 900001],
                  ['taobao', 900002],
                ].map(([platform, id]) => ({
                  ...data[0],
                  id,
                  platform,
                  name: '同名测试店',
                  alias: '同名测试店',
                  detected_name: '同名测试店',
                  username: 'ui-fixture',
                  state: 'logged_in',
                })),
              ];
            else
              data = {
                ...data,
                rows: [
                  {
                    id: 9001,
                    shop_id: 900001,
                    shop_name: '同名测试店',
                    platform: 'pdd',
                    level: 'info',
                    module: 'login',
                    message: '界面身份验证',
                    last_seen: Date.now() / 1000,
                  },
                  {
                    id: 9002,
                    shop_id: 900002,
                    shop_name: '同名测试店',
                    platform: 'taobao',
                    level: 'info',
                    module: 'login',
                    message: '界面身份验证',
                    last_seen: Date.now() / 1000,
                  },
                  {
                    id: 9003,
                    shop_id: null,
                    shop_name: '',
                    platform: '',
                    level: 'info',
                    module: 'system',
                    message: '系统事件',
                    last_seen: Date.now() / 1000,
                  },
                ],
              };
            await call('Fetch.fulfillRequest', {
              requestId: p.requestId,
              responseCode: 200,
              responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
              body: Buffer.from(JSON.stringify(data)).toString('base64'),
            });
            return;
          }
          await call('Fetch.continueRequest', { requestId: p.requestId });
        } catch (error) {
          errors.push(error.message);
          await call('Fetch.continueRequest', { requestId: p.requestId }).catch(() => {});
        }
      }
    });
    await call('Fetch.enable', {
      patterns: [
        { urlPattern: 'http://127.0.0.1:3210/api/shops', requestStage: 'Response' },
        { urlPattern: 'http://127.0.0.1:3210/api/logs?*', requestStage: 'Response' },
      ],
    });
    await wait("!!document.querySelector('.workspace-sidebar')");
    await evaluate(
      "localStorage.setItem('pdd-manager:view','overview');localStorage.setItem('pdd-manager:auto-refresh','false');location.reload()",
    );
    await wait('!!document.querySelector(\'[aria-label="概览店铺"] option[value="900002"]\')');
    await assert(
      'document.querySelector(\'[aria-label="概览店铺"] option[value="900001"]\').textContent===\'拼多多 · 同名测试店\'&&document.querySelector(\'[aria-label="概览店铺"] option[value="900002"]\').textContent===\'淘宝 · 同名测试店\'',
      'Same-name shop dropdowns show platform and keep different IDs',
    );
    await assert(
      "[...document.querySelectorAll('.shop-comparison-head strong')].some(e=>e.textContent==='拼多多 · 同名测试店')&&[...document.querySelectorAll('.shop-comparison-head strong')].some(e=>e.textContent==='淘宝 · 同名测试店')",
      'Trend cards distinguish same-name stores',
    );
    await assert(
      "[...document.querySelectorAll('.ov-store-name strong')].some(e=>e.textContent==='淘宝 · 同名测试店')",
      'Operating detail table shows platform prefix',
    );
    await click('系统管理');
    await wait("!!document.querySelector('.shop-card')");
    await assert(
      "[...document.querySelectorAll('.shop-card h3')].some(e=>e.textContent==='淘宝 · 同名测试店')",
      'Shop management cards use same identity',
    );
    await evaluate('document.querySelector(\'[aria-label="淘宝 · 同名测试店更多操作"]\').click()');
    await evaluate(
      "[...document.querySelectorAll('.shop-card')].find(e=>e.textContent.includes('淘宝 · 同名测试店')).querySelector('.shop-card-menu-actions button').click()",
    );
    await wait("!!document.querySelector('.shop-editor')");
    await assert(
      "document.querySelector('.shop-editor input[maxlength=\"100\"]').value==='同名测试店'&&document.querySelector('.shop-editor').textContent.includes('淘宝 · 同名测试店')",
      'Editing keeps raw shop name while identifying its platform',
    );
    await evaluate('document.querySelector(\'.shop-editor [aria-label="关闭"]\').click()');
    await click('管理运营人员 / 批量分配');
    await wait("!!document.querySelector('.operator-shop-list')");
    await assert(
      "document.querySelector('.operator-shop-list').textContent.includes('拼多多 · 同名测试店')&&document.querySelector('.operator-shop-list').textContent.includes('淘宝 · 同名测试店')",
      'Operator assignment distinguishes platforms',
    );
    await evaluate("document.querySelector('.operator-dialog .dialog-head button').click()");
    await click('运行日志');
    await wait("document.querySelectorAll('.log-table tbody tr').length===3");
    await assert(
      "[...document.querySelectorAll('.log-table tbody td:nth-child(3)')].map(e=>e.textContent).join('|')==='拼多多 · 同名测试店|淘宝 · 同名测试店|系统'",
      'Logs distinguish same names and keep system events separate',
    );
    await assert(
      'document.querySelector(\'[aria-label="日志店铺"] option[value="900002"]\').textContent===\'淘宝 · 同名测试店\'',
      'Log dropdown shows platform prefix',
    );
    await capture('shop-identity-logs.png');
    await evaluate(
      "(()=>{const e=document.querySelector('[aria-label=\"日志店铺\"]');e.value='900002';e.dispatchEvent(new Event('change',{bubbles:true}));})()",
    );
    await pause(700);
    if (!requests.some((url) => url.includes('/api/logs?') && url.includes('shop_id=900002')))
      throw Error('Log filter lost shop ID');
    console.log('PASS log filter targets the selected shop ID');
    await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
    await pause(150);
    await assert('document.documentElement.scrollWidth<=innerWidth+1', 'Shop identity labels fit mobile layout');
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL shop identity UI checks passed');
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
