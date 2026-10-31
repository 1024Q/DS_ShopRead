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
      '--remote-debugging-port=19356',
      `--user-data-dir=${path.join(root, '.data/ui-package-interval')}`,
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19356/json/list')).json()).find((t) => t.type === 'page');
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
        "if(location.origin==='http://127.0.0.1:3210'&&!sessionStorage.packageInterval){localStorage.setItem('pdd-manager:view','settings');localStorage.setItem('pdd-manager:auto-refresh','false');sessionStorage.packageInterval='1';}",
    });
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait("document.querySelector('[aria-label=\"自定义采集间隔\"]')?.value==='300'", 60);
    await assert(
      "document.querySelector('[aria-label=\"自定义采集间隔\"] option:checked').textContent==='每 5 分钟'",
      'Saved five-minute interval is shown in settings',
    );
    await assert(
      "!document.body.innerText.includes('单独采集间隔')&&document.querySelector('[aria-label=\"包裹中心采集方式\"]')?.value==='custom'",
      'Only the module selector controls the custom interval',
    );
    await assert(
      "document.querySelector('[aria-label=\"自定义采集间隔\"]').closest('section').querySelector('h3').textContent==='后台采集计划'",
      'Custom interval is inside the collection plan',
    );
    await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent==='保存采集设置').click()");
    await wait("document.querySelector('[role=status].notice')?.textContent.includes('设置已保存')");
    await evaluate('location.reload()');
    await wait(
      "document.querySelector('[aria-label=\"包裹中心采集方式\"]')?.value==='custom'&&document.querySelector('[aria-label=\"自定义采集间隔\"]')?.value==='300'",
    );
    console.log('PASS custom interval and module choice survive save and reload');
    await evaluate(
      "document.querySelector('[aria-label=\"自定义采集间隔\"]').closest('section').scrollIntoView({block:'start'})",
    );
    await pause(250);
    fs.writeFileSync(
      path.join(root, '.data/custom-interval-settings.png'),
      Buffer.from((await call('Page.captureScreenshot', { format: 'png' })).data, 'base64'),
    );
    await evaluate("localStorage.setItem('pdd-manager:view','sources');location.reload()");
    await wait('!!document.querySelector(\'[aria-label="数据源模块"] option[value="packages"]\')', 60);
    await evaluate(
      "(()=>{const e=document.querySelector('[aria-label=\"数据源模块\"]');e.value='packages';e.dispatchEvent(new Event('change',{bubbles:true}));})()",
    );
    await pause(500);
    await assert(
      "(()=>{const rows=[...document.querySelectorAll('.sync-table tbody tr')].filter(r=>r.textContent.includes('拼多多'));return rows.length>0&&rows.every(r=>r.textContent.includes('自定义采集间隔')&&r.textContent.includes('每 5 分钟'));})()",
      'Pinduoduo package sources use five-minute intervals',
    );
    await assert(
      "(()=>{const rows=[...document.querySelectorAll('.sync-table tbody tr')].filter(r=>r.textContent.includes('淘宝'));return rows.length>0&&rows.every(r=>r.textContent.includes('每 1 分钟'));})()",
      'Taobao package interval remains one minute',
    );
    if (errors.length) throw Error(errors.join('\n'));
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
