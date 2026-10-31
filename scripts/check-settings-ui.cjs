const { spawn } = require('node:child_process');
const fs = require('node:fs'),
  path = require('node:path'),
  http = require('node:http');
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const root = path.resolve(__dirname, '..'),
    created = [],
    events = [];
  const fixtureToken = 'isolated-ui-fixture-secret';
  const servers = [19877, 19878].map((port, i) =>
    http.createServer(async (req, res) => {
      if (req.headers.authorization !== 'Bearer ' + fixtureToken) {
        res.writeHead(401);
        res.end('{}');
        return;
      }
      let body = '';
      for await (const part of req) body += part;
      const url = new URL(req.url, 'http://fixture');
      events.push({ port, path: url.pathname, method: req.method, body });
      res.setHeader('Content-Type', 'application/json');
      if (url.pathname === '/api/shops') {
        res.end(
          JSON.stringify([
            {
              id: 1,
              name: '远程测试机器' + (i ? '乙' : '甲'),
              platform: 'pdd',
              state: 'logged_in',
              adapter_available: true,
              username: 'fixture-account',
              browser: 'system',
              visible: false,
            },
          ]),
        );
        return;
      }
      if (url.pathname === '/api/browsers') {
        res.end(JSON.stringify([{ id: 'system', available: true }]));
        return;
      }
      if (url.pathname === '/api/shops/1/screenshot') {
        res.setHeader('Content-Type', 'image/png');
        res.end(
          Buffer.from(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1ioAAAAASUVORK5CYII=',
            'base64',
          ),
        );
        return;
      }
      if (url.pathname === '/api/system/status') {
        res.end(
          JSON.stringify({
            sources: [],
            summary: { running: 0, queued: 0, errors: 0 },
            collection: { enabled: true, realtime_seconds: 60 },
          }),
        );
        return;
      }
      res.end(JSON.stringify({ ok: true, rows: [], sources: [] }));
    }),
  );
  await Promise.all(servers.map((s, i) => new Promise((r) => s.listen(19877 + i, '127.0.0.1', r))));
  const chrome = spawn(
    path.join(root, '.browsers/chrome-win64/chrome.exe'),
    [
      '--headless=new',
      '--no-first-run',
      '--window-size=1560,1100',
      '--remote-debugging-port=19326',
      `--user-data-dir=${path.join(root, '.data/ui-settings-test')}`,
      'http://127.0.0.1:3210/',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call, evaluate;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19326/json/list')).json()).find((t) => t.type === 'page');
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
    evaluate = async (expression) => {
      const r = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
      return r.result.value;
    };
    const wait = async (exp, seconds = 35) => {
      for (let n = 0; n < seconds * 2; n++) {
        if (await evaluate(exp)) return;
        await pause(500);
      }
      throw Error('Timed out: ' + exp);
    };
    const click = async (text) => {
      await evaluate(
        `(()=>{const b=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()===${JSON.stringify(text)}||e.getAttribute('aria-label')===${JSON.stringify(text)});if(!b)throw Error('Missing button '+${JSON.stringify(text)});b.click();})()`,
      );
      await pause(250);
    };
    const assert = async (exp, label) => {
      if (!(await evaluate(exp))) throw Error(label);
      console.log('PASS', label);
    };
    const set = async (selector, value) => {
      await evaluate(
        `(()=>{const e=document.querySelector(${JSON.stringify(selector)});Object.getOwnPropertyDescriptor(e.tagName==='SELECT'?HTMLSelectElement.prototype:HTMLInputElement.prototype,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event(e.tagName==='SELECT'?'change':'input',{bubbles:true}));})()`,
      );
      await pause(200);
    };
    const capture = async (name) =>
      fs.writeFileSync(
        path.join(root, '.data', name),
        Buffer.from((await call('Page.captureScreenshot', { format: 'png' })).data, 'base64'),
      );
    await wait("!!document.querySelector('.workspace-sidebar')");
    await evaluate(
      "localStorage.setItem('pdd-manager:view','settings');localStorage.setItem('pdd-manager:theme','forest');localStorage.setItem('pdd-manager:auto-refresh','false');location.reload()",
    );
    await wait("document.querySelectorAll('.module-settings select').length===20");
    await assert(
      "!document.querySelector('header').textContent.includes('API 连接设置')&&document.querySelector('.settings-page').textContent.includes('API 连接设置')",
      'API connection lives in system settings',
    );
    await assert(
      "document.querySelector('input[type=time]').value==='03:00'&&[...document.querySelectorAll('.settings-grid select')].some(s=>s.value==='60')",
      'Server schedule uses one minute and Beijing 03:00',
    );
    await click('深色');
    await assert("document.documentElement.dataset.theme==='dark'", 'Dark theme applied');
    await capture('settings-dark.png');
    await evaluate('location.reload()');
    await wait("document.documentElement.dataset.theme==='dark'&&!!document.querySelector('.settings-page')");
    await assert("localStorage.getItem('pdd-manager:theme')==='dark'", 'Theme persists across reload');
    await click('数据中心');
    await wait("!!document.querySelector('.overview-dashboard')");
    await capture('overview-dark.png');
    await assert(
      "[...document.querySelectorAll('.ov-kpi')].every(e=>getComputedStyle(e).backgroundColor!=='rgb(255, 255, 255)')",
      'All overview cards honor dark theme',
    );
    if (process.argv.includes('--theme-only')) return;
    await click('系统设置');
    await wait("!!document.querySelector('.settings-page')");
    await click('暖沙');
    await capture('settings-sand.png');
    await click('墨绿');
    await click('保存采集设置');
    await wait("document.querySelector('.settings-page').textContent.includes('设置已保存')");
    await click('店铺管理');
    await set('[aria-label="平台分类"]', 'taobao');
    await assert("document.querySelectorAll('.cards .card').length===0", 'Platform filter isolates shops');
    await click('＋ 添加店铺');
    await assert(
      "[...document.querySelectorAll('.dialog select option')].some(o=>o.textContent==='微信小店')",
      'Shop editor supports platform categories',
    );
    await click('关闭');
    await click('远程机器');
    await wait("document.querySelector('.business-heading h2')?.textContent==='远程机器'");
    for (let n = 0; n < 2; n++) {
      await click('＋ 添加机器');
      await set('[aria-label="机器连接配置"] input:not([type])', '测试远程机器' + n);
      await set('[aria-label="机器连接配置"] input[type=url]', `http://127.0.0.1:${19877 + n}`);
      await set('[aria-label="机器连接配置"] input[type=password]', fixtureToken);
      await click('测试连接并保存');
      await wait('!document.querySelector(\'[aria-label="机器连接配置"]\')');
      const nodes = await evaluate("fetch('/api/nodes').then(r=>r.json())");
      const node = nodes.find((x) => x.url === `http://127.0.0.1:${19877 + n}`);
      if (!node) throw Error('Node not saved');
      created.push(node.id);
    }
    await wait("document.querySelectorAll('.remote-shop').length>=2");
    await assert(
      "!document.querySelector('.page-content').textContent.includes('isolated-ui-fixture-secret')",
      'Remote credential is not exposed after save',
    );
    await capture('remote-machines.png');
    await click('远程控制');
    await wait("!!document.querySelector('.viewer img')");
    await set('.viewer .input-row input', 'manual-code');
    await click('输入到浏览器');
    await click('回车');
    await click('退格');
    await pause(400);
    if (!events.some((e) => e.port === 19877 && e.path.endsWith('/input') && JSON.parse(e.body).text === 'manual-code'))
      throw Error('Remote input did not reach machine A');
    console.log('PASS Remote screenshot and input target the selected shop');
    await click('关闭画面');
    await set('[aria-label="工作机器"]', String(created[1]));
    await wait('document.querySelector(\'[aria-label="工作机器"]\')?.value===' + JSON.stringify(String(created[1])));
    await click('店铺管理');
    await set('[aria-label="平台分类"]', '');
    await wait("document.querySelector('.cards')?.textContent.includes('远程测试机器乙')");
    await assert(
      "!document.querySelector('.cards').textContent.includes('远程测试机器甲')",
      'Same shop ID is routed by active machine',
    );
    await set('[aria-label="工作机器"]', '0');
    await wait("document.querySelector('[aria-label=\"工作机器\"]')?.value==='0'");
    await click('系统设置');
    await wait("!!document.querySelector('.settings-page')");
    await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
    await pause(350);
    await assert('document.documentElement.scrollWidth<=innerWidth+1', 'System settings fits mobile viewport');
    await capture('settings-mobile.png');
    if (errors.length) throw Error(errors.join('\n'));
    console.log('System settings and remote machine checks passed');
  } finally {
    const token = fs.readFileSync(path.join(root, '.data/api.key'), 'utf8').trim();
    for (const id of created)
      await fetch(`http://127.0.0.1:8000/api/nodes/${id}`, {
        method: 'DELETE',
        headers: { Authorization: 'Bearer ' + token },
      }).catch(() => {});
    if (call) {
      await call('Browser.close').catch(() => {});
      ws.close();
    } else chrome.kill();
    await Promise.all(servers.map((s) => new Promise((r) => s.close(r))));
  }
})().catch((e) => {
  console.error(e.stack || e.message);
  process.exitCode = 1;
});
