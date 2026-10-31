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
      '--remote-debugging-port=19361',
      `--user-data-dir=${path.join(root, '.data/ui-dual-retry')}`,
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19361/json/list')).json()).find((t) => t.type === 'page');
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

    await wait(
      "[...document.querySelectorAll('.ov-dual-metrics .ov-revenue-value')].filter(e=>e.textContent.includes('¥')).length===2",
      60,
    );
    await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent==='关闭画面')?.click()");
    await assert(
      "document.querySelectorAll('.ov-revenue [data-basis]').length===2&&!document.querySelector('.ov-revenue-basis button')",
      'Both bases visible without switching',
    );
    await pause(800);
    const before = requests.length;
    await evaluate('window.advanceRefresh()');
    await pause(3000);
    for (const kind of ['overview', 'transactions']) {
      if (!requests.slice(before).some((url) => url.includes('/api/data/' + kind + '?')))
        throw Error('Automatic refresh missing ' + kind);
      console.log('PASS automatic refresh', kind);
    }
    const forceBefore = syncCommands.length;
    await evaluate("[...document.querySelectorAll('header button')].find(b=>b.textContent==='立即同步').click()");
    await pause(1000);
    for (const kind of ['overview', 'transactions']) {
      if (
        !syncCommands
          .slice(forceBefore)
          .some((url) => url.includes('/api/data/' + kind + '/sync') && url.includes('force=true'))
      )
        throw Error('Manual sync missing ' + kind);
      console.log('PASS manual sync', kind);
    }
    const shot = async (name) => {
      await pause(200);
      fs.writeFileSync(
        path.join(root, '.data', name),
        Buffer.from((await call('Page.captureScreenshot', { format: 'png' })).data, 'base64'),
      );
    };
    await shot('dual-revenue-desktop.png');
    await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 950, deviceScaleFactor: 1, mobile: false });
    await evaluate("document.querySelector('.ov-dual-revenue').scrollIntoView({block:'center'})");
    await pause(250);
    await assert(
      "[...document.querySelectorAll('.ov-dual-metrics .ov-revenue-value')].every(e=>e.scrollWidth<=e.clientWidth+1)",
      'Both amounts fit mobile cards',
    );
    await shot('dual-revenue-mobile.png');
    await call('Emulation.clearDeviceMetricsOverride');
    await evaluate(
      `(async()=>{const value=await fetch('/api/system/status').then(r=>r.json()),base=value.sources[0];window.retryStatus={...value,sources:[{...base,shop_name:'测试等待重试',shop_label:'拼多多 · 测试等待重试',shop_id:9001,kind:'products',syncing:false,data_status:'error',error:'平台返回异常',retry_at:Date.now()/1000+60,retry_wait_seconds:60,failures:2,retry_enabled:true},{...base,shop_name:'测试正在重试',shop_label:'拼多多 · 测试正在重试',shop_id:9002,kind:'overview',syncing:true,data_status:'running',sync_phase:'running',sync_stage:'重新读取',error:null,previous_error:'上一轮页面异常',sync_loaded:5,sync_total:10,elapsed_seconds:2}]};const original=window.fetch;window.fetch=(url,opts)=>url==='/api/system/status'?Promise.resolve(new Response(JSON.stringify(window.retryStatus),{headers:{'Content-Type':'application/json'}})):original(url,opts);})()`,
    );
    await evaluate('document.querySelector(\'.workspace-sidebar [aria-label="系统管理"]\').click()');
    await evaluate(
      "[...document.querySelectorAll('.workspace-sidebar button')].find(b=>b.textContent==='同步与数据源').click()",
    );
    await wait("!!document.querySelector('.sync-center')");
    await evaluate('window.advanceRefresh()');
    await wait("document.querySelector('.sync-center').textContent.includes('约 60 秒后自动重试')");
    await assert(
      "document.querySelector('.sync-center').textContent.includes('重新读取')&&document.querySelector('.sync-center').textContent.includes('已读取 5 / 10')",
      'Current retry progress shown alongside previous error',
    );
    await shot('retry-status-ui.png');
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL dual refresh and retry UI checks passed');
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
