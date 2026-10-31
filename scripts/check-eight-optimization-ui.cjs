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
      '--remote-debugging-port=19365',
      `--user-data-dir=${path.join(root, '.data/ui-eight-optimization')}`,
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19365/json/list')).json()).find((t) => t.type === 'page');
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

    const syncCommands = [];
    ws.addEventListener('message', async (e) => {
      const m = JSON.parse(e.data);
      if (m.method === 'Fetch.requestPaused') {
        const p = m.params;
        try {
          syncCommands.push(p.request.url);
          await call('Fetch.fulfillRequest', {
            requestId: p.requestId,
            responseCode: 202,
            responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
            body: Buffer.from('{}').toString('base64'),
          });
        } catch (e) {
          if (!/Invalid InterceptionId/i.test(e.message)) errors.push(e.message);
        }
      }
    });
    await call('Runtime.enable');
    await call('Page.enable');
    await call('Fetch.enable', { patterns: [{ urlPattern: '*/api/data/*/sync*' }] });
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source:
        "if(location.origin==='http://127.0.0.1:3210'&&!sessionStorage.headerTools){localStorage.setItem('pdd-manager:view','overview');localStorage.setItem('pdd-manager:filters:products',JSON.stringify({draft:{},pageSize:20}));localStorage.setItem('pdd-manager:auto-refresh','true');sessionStorage.headerTools='1';}",
    });

    await call('Network.enable');
    ws.addEventListener('message', (e) => {
      const m = JSON.parse(e.data);
      if (m.method === 'Network.requestWillBeSent' && m.params.request.method === 'GET')
        requests.push(m.params.request.url);
    });
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source: `(()=>{const set=window.setInterval,clear=window.clearInterval,timers=new Map();window.setInterval=(f,ms,...args)=>{const id=set(f,ms,...args);if(ms===3000||ms===5000)timers.set(id,f);return id;};window.clearInterval=id=>{timers.delete(id);return clear(id);};window.advanceRefresh=()=>{const original=Date.now;Date.now=()=>original()+65000;try{for(const f of timers.values())f();}finally{Date.now=original;}};})()`,
    });
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });

    await wait("document.querySelector('.ov-revenue-value')?.textContent.includes('¥')", 60);
    await pause(800);
    await assert("!!document.querySelector('.data-coverage summary')", 'Compact data completeness visible');
    await evaluate(
      `(()=>{const original=window.fetch;window.holdOverview=false;window.overviewHeld=false;window.releaseOverview=null;window.fetch=async(...args)=>{const result=await original(...args);if(window.holdOverview&&String(args[0]).includes('/api/data/overview?')){window.overviewHeld=true;await new Promise(r=>window.releaseOverview=r);}return result;};window.holdOverview=true;window.advanceRefresh();})()`,
    );
    await wait('window.overviewHeld');
    const beforeForce = syncCommands.filter((u) => u.includes('/overview/sync') && u.includes('force=true')).length;
    await evaluate(
      "(()=>{const b=[...document.querySelectorAll('.global-sync-actions button')].find(b=>b.textContent==='立即同步');b.click();b.click();b.click();})()",
    );
    await pause(250);
    if (syncCommands.filter((u) => u.includes('/overview/sync') && u.includes('force=true')).length !== beforeForce)
      throw Error('Force did not wait for active read');
    await evaluate('window.holdOverview=false;window.releaseOverview()');
    await pause(2000);
    if (syncCommands.filter((u) => u.includes('/overview/sync') && u.includes('force=true')).length !== beforeForce + 1)
      throw Error('Queued force missing or repeated');
    console.log('PASS Busy manual refresh is remembered and coalesced');
    await wait('!!document.querySelector(\'.workspace-sidebar [aria-label="系统管理"]\')', 60);
    await evaluate('document.querySelector(\'.workspace-sidebar [aria-label="系统管理"]\').click()');
    await wait("[...document.querySelectorAll('.workspace-sidebar button')].some(b=>b.textContent==='同步与数据源')");
    await evaluate(
      "[...document.querySelectorAll('.workspace-sidebar button')].find(b=>b.textContent==='同步与数据源').click()",
    );
    await wait("!!document.querySelector('.queue-diagnostics')", 60);
    await assert(
      "document.querySelector('.collection-queues').textContent.includes('本机有效并发')",
      'Machine load and effective concurrency visible',
    );
    await assert(
      "[...document.querySelectorAll('.sync-table button')].some(b=>b.textContent==='重新采集')",
      'Per-source recovery action visible',
    );
    await assert(
      "document.querySelector('.queue-health').textContent.includes('处理速度')&&document.querySelector('.queue-health').textContent.includes('重试占用')",
      'Live queue health diagnostics',
    );
    await evaluate(
      "[...document.querySelectorAll('.queue-health button')].find(b=>b.textContent==='查看任务明细').click()",
    );
    await assert(
      "document.querySelectorAll('.queue-diagnostics tbody tr').length>0",
      'Actual queue tasks with waiting reasons',
    );
    await evaluate("document.querySelector('.collection-queues').scrollIntoView({block:'start'})");
    fs.writeFileSync(
      path.join(root, '.data/queue-optimization.png'),
      Buffer.from((await call('Page.captureScreenshot', { format: 'png' })).data, 'base64'),
    );
    await evaluate(
      "[...document.querySelectorAll('.workspace-sidebar button')].find(b=>b.textContent==='系统设置').click()",
    );
    await wait('!!document.querySelector(\'[aria-label="店铺错峰窗口"]\')');
    await assert(
      "document.querySelector('[aria-label=\"店铺错峰窗口\"]').value==='60'&&document.querySelector('[aria-label=\"异常重试最大并发\"]').value==='1'",
      'Stagger and retry defaults visible',
    );
    await assert(
      '!!document.querySelector(\'[aria-label="每批最多读取页数"]\')&&!!document.querySelector(\'[aria-label="机器负载自动调节"]\')',
      'Batch and load policy controls',
    );
    await wait('!!document.querySelector(\'[aria-label="数据库备份"] table\')', 60);
    await assert(
      "document.querySelector('[aria-label=\"数据库备份\"]').textContent.includes('当前不自动删除')",
      'Backup validation and retention preview',
    );
    await evaluate("document.querySelector('[aria-label=\"数据库备份\"]').scrollIntoView({block:'start'})");
    fs.writeFileSync(
      path.join(root, '.data/eight-settings.png'),
      Buffer.from((await call('Page.captureScreenshot', { format: 'png' })).data, 'base64'),
    );
    await evaluate('document.querySelector(\'.workspace-sidebar [aria-label="推广平台"]\').click()');
    await wait("[...document.querySelectorAll('.workspace-sidebar button')].some(b=>b.textContent==='商品推广')");
    await evaluate(
      "[...document.querySelectorAll('.workspace-sidebar button')].find(b=>b.textContent==='商品推广').click()",
    );
    await wait("!!document.querySelector('.promotions-panel .data-coverage summary')", 60);
    await assert(
      "document.querySelector('.promotions-panel .data-coverage').textContent.includes('重新同步当前范围')||document.querySelector('.promotions-panel .data-coverage').textContent.includes('追加一次同步请求')",
      'Promotion coverage and refresh action visible',
    );
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL eight optimization UI checks passed');
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
