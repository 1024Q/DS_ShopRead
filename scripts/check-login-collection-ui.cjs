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
      if (m.method === 'Network.requestWillBeSent')
        requests.push({ url: m.params.request.url, method: m.params.request.method });
      if (m.method === 'Fetch.requestPaused') {
        const p = m.params;
        if (p.request.method === 'POST')
          await call('Fetch.fulfillRequest', {
            requestId: p.requestId,
            responseCode: 202,
            responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
            body: Buffer.from(JSON.stringify({ tasks: [] })).toString('base64'),
          });
        else await call('Fetch.continueRequest', { requestId: p.requestId });
      }
    });
    await call('Fetch.enable', { patterns: [{ urlPattern: '*/api/data/*/sync*', requestStage: 'Request' }] });
    await wait("!!document.querySelector('.workspace-sidebar')");
    await evaluate("localStorage.setItem('pdd-manager:view','overview');location.reload()");
    await wait('!!document.querySelector(\'[aria-label="概览店铺"]\')');
    await pause(1500);
    if (!requests.some((r) => r.method === 'POST' && r.url.includes('/sync?')))
      throw Error('Missing independent collection command');
    if (requests.some((r) => r.method === 'GET' && r.url.includes('/api/data/') && /[?&](refresh|force)=/.test(r.url)))
      throw Error('Read URL still controls collection');
    console.log('PASS initial demand uses POST and database reads use GET');
    await click('立即同步');
    await pause(1000);
    if (!requests.some((r) => r.method === 'POST' && r.url.includes('force=true')))
      throw Error('Manual sync did not send force command');
    console.log('PASS manual sync sends independent command');
    await click('系统管理');
    await wait("!!document.querySelector('.shop-card')");
    await assert(
      "[...document.querySelectorAll('.shop-card h3')].some(e=>e.textContent==='淘宝 · 蓝怡宝旗舰店')",
      'Existing Taobao shop name is corrected',
    );
    await click('＋ 添加店铺');
    await wait("!!document.querySelector('.shop-editor')");
    for (const platform of ['pdd', 'taobao', 'jd', 'douyin', 'taofactory', 'xiaohongshu', 'wechat', 'other']) {
      await evaluate(
        `(()=>{const e=[...document.querySelectorAll('.shop-editor label')].find(e=>e.textContent.startsWith('所属平台')).querySelector('select');e.value=${JSON.stringify(platform)};e.dispatchEvent(new Event('change',{bubbles:true}));})()`,
      );
      await pause(80);
      await assert(
        'document.querySelector(\'[aria-label="店铺登录方式"]\').options.length===2',
        platform + ' has explicit login choices',
      );
      if (platform === 'wechat')
        await assert(
          "document.querySelector('[aria-label=\"店铺登录方式\"] option[value=password]').disabled&&document.querySelector('[aria-label=\"店铺登录方式\"]').value==='qr'",
          'Wechat labels unsupported password method',
        );
      else {
        await evaluate(
          "(()=>{const e=document.querySelector('[aria-label=\"店铺登录方式\"]');e.value='password';e.dispatchEvent(new Event('change',{bubbles:true}));})()",
        );
        await pause(80);
        await assert(
          "!!document.querySelector('.shop-editor input[type=password]')",
          platform + ' password input available',
        );
        await evaluate(
          "(()=>{const e=document.querySelector('[aria-label=\"店铺登录方式\"]');e.value='qr';e.dispatchEvent(new Event('change',{bubbles:true}));})()",
        );
        await pause(80);
        await assert(
          "!document.querySelector('.shop-editor input[type=password]')",
          platform + ' QR mode needs no password',
        );
      }
    }
    await capture('login-methods-ui.png');
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL login and collection UI checks passed');
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
