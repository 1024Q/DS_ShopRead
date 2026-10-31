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
      '--remote-debugging-port=19337',
      `--user-data-dir=${path.join(root, '.data/ui-workspace-updates')}`,
      'http://127.0.0.1:3210/',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19337/json/list')).json()).find((t) => t.type === 'page');
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

    await wait("!!document.querySelector('.workspace-sidebar')");
    const view = async (name) => {
      await evaluate(
        `localStorage.setItem('pdd-manager:auto-refresh','false');localStorage.setItem('pdd-manager:view',${JSON.stringify(name)});localStorage.removeItem('pdd-manager:filters:'+${JSON.stringify(name)});location.reload()`,
      );
      await pause(400);
    };
    await view('overview');
    await wait(
      "document.querySelectorAll('.shop-comparison').length===20&&document.querySelectorAll('.arrival-months button').length===12",
    );
    await assert(
      "document.querySelector('[aria-label=\"趋势店铺\"]').value===''",
      'Trade trend defaults to every shop',
    );
    await assert(
      "document.querySelectorAll('.shop-comparison').length===20&&!document.querySelector('.shop-comparisons .pagination')",
      'All shop trends render without pagination',
    );
    await assert("document.querySelector('.workspace-header').getBoundingClientRect().height<=70", 'Compact header');
    await assert(
      "document.querySelector('.arrival-comparison').textContent.includes('今日上新')&&document.querySelector('.arrival-comparison').textContent.includes('昨日上新')",
      'Daily arrivals default today versus yesterday',
    );
    await capture('v3-overview.png');
    await evaluate("document.querySelector('.new-products-panel').scrollIntoView({block:'start'})");
    await capture('v3-arrivals.png');
    await evaluate("document.querySelector('.shop-comparisons').scrollIntoView({block:'start'})");
    const box = await evaluate(
      "(()=>{const r=document.querySelector('.shop-comparison').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()",
    );
    await call('Input.dispatchMouseEvent', { type: 'mouseMoved', ...box });
    await pause(300);
    await assert(
      "getComputedStyle(document.querySelector('.shop-comparison')).transform!=='none'",
      'Hover enlarges the selected trend card',
    );
    await capture('v3-trend-hover.png');
    await evaluate("document.querySelector('.arrival-comparison button').click()");
    await wait("document.querySelector('.business-heading h2')?.textContent==='商品列表'");
    await assert(
      "[...document.querySelectorAll('.filter-panel input[type=date]')].every(e=>e.value===new Date(Date.now()+8*3600000).toISOString().slice(0,10))",
      'Arrival click opens matching product creation dates',
    );
    await view('products');
    await wait("document.querySelector('.data-table tbody tr')&&document.querySelectorAll('.column-sort').length>3");
    await evaluate("[...document.querySelectorAll('.column-sort')].find(e=>e.textContent==='库存').click()");
    await wait("document.querySelector('th[aria-sort=ascending]')?.textContent==='库存'");
    await pause(1200);
    await assert(
      "(()=>{const vals=[...document.querySelectorAll('.data-table tbody tr')].map(r=>Number(r.cells[4].innerText.replaceAll(',','')));return vals.every((v,i)=>!i||v>=vals[i-1])})()",
      'Product stock sorts ascending across API result',
    );
    await evaluate("[...document.querySelectorAll('.column-sort')].find(e=>e.textContent==='库存').click()");
    await pause(1200);
    await assert(
      "document.querySelector('th[aria-sort=descending]')?.textContent==='库存'",
      'Second header click reverses ordering',
    );
    await assert("!!document.querySelector('.column-resizer')", 'Column resizing survives header sorting');
    await capture('v3-sorted-table.png');
    await view('shops');
    await wait("document.querySelectorAll('.shop-owner select').length===20");
    await click('管理运营人员 / 批量分配');
    await wait("!!document.querySelector('.operator-dialog')");
    await assert(
      "document.querySelectorAll('.operator-shop-list label').length===20",
      'Operator manager lists all shops for batch assignment',
    );
    await capture('v3-operators.png');
    await click('关闭');
    await click('＋ 添加店铺');
    await wait("!!document.querySelector('.shop-editor')");
    for (const platform of ['taobao', 'jd', 'douyin', 'taofactory', 'xiaohongshu', 'wechat', 'other']) {
      await evaluate(
        `(()=>{const e=document.querySelector('.shop-editor select');e.value=${JSON.stringify(platform)};e.dispatchEvent(new Event('change',{bubbles:true}));})()`,
      );
      await pause(100);
      await assert(
        "document.querySelector('[aria-label=\"店铺登录方式\"]').value==='qr'&&!document.querySelector('.shop-editor input[type=password]')",
        platform + ' QR mode available without password',
      );
    }
    await capture('v3-qr-platforms.png');
    await view('overview');
    await wait("!!document.querySelector('.overview-dashboard')");
    await wait(
      "document.querySelectorAll('.shop-comparison').length===20&&document.querySelectorAll('.arrival-months button').length===12",
    );
    for (const theme of ['white', 'dark', 'blue']) {
      await evaluate(`document.documentElement.dataset.theme=${JSON.stringify(theme)}`);
      await evaluate('window.scrollTo(0,0)');
      await capture('v3-theme-' + theme + '.png');
      await assert(
        "[...document.querySelectorAll('select')].every(e=>getComputedStyle(e).appearance==='none')",
        theme + ' uniform dropdowns',
      );
    }
    await view('logs');
    await wait("!!document.querySelector('.log-panel')||document.body.innerText.includes('运行日志')");
    await evaluate("document.querySelector('.sidebar-brand').click()");
    await wait("!!document.querySelector('.overview-dashboard')");
    await assert("!!document.querySelector('.overview-dashboard')", 'Logo returns home');
    await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
    await pause(300);
    await assert('document.documentElement.scrollWidth<=innerWidth+1', 'Mobile page fits screen');
    await capture('v3-mobile.png');
    if (errors.length) throw Error('Browser errors: ' + errors.join('\n'));
    console.log('ALL console feature UI checks passed');
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
