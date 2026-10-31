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
      '--remote-debugging-port=19324',
      `--user-data-dir=${path.join(root, '.data/ui-overview-test')}`,
      'http://127.0.0.1:3210/',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19324/json/list')).json()).find((t) => t.type === 'page');
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
        `(()=>{const b=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()===${JSON.stringify(text)}||e.getAttribute('aria-label')===${JSON.stringify(text)});if(!b)throw Error('Missing button '+${JSON.stringify(text)});b.click();})()`,
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
    await evaluate(
      "localStorage.setItem('pdd-manager:auto-refresh','false');localStorage.setItem('pdd-manager:view','overview');localStorage.removeItem('pdd-manager:filters:overview');location.reload()",
    );
    await wait("document.querySelectorAll('.ov-store-table tbody tr').length===2");
    await wait("!!document.querySelector('.ov-line-chart')", 90);
    await assert("document.querySelector('.ov-revenue-value').textContent.startsWith('¥')", 'Live revenue shown');
    await assert("document.querySelectorAll('.ov-donut-wrap circle').length===3", 'Two real shops contribute to donut');
    await assert(
      "document.querySelectorAll('.ov-line-chart polyline').length===2",
      'Today and yesterday real trade series rendered',
    );
    await call('Emulation.setDeviceMetricsOverride', {
      width: 1560,
      height: 1600,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await pause(250);
    await evaluate("history.replaceState(null,'','/');window.scrollTo({top:0,behavior:'instant'})");
    await pause(250);
    await capture('overview-desktop.png');
    await click('订单数');
    await assert(
      "document.querySelector('.ov-segments button[aria-pressed=true]').textContent==='订单数'",
      'Trend metric switches',
    );
    await evaluate(
      "var slider=document.querySelector('[aria-label=\"查看趋势小时\"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(slider,'8');slider.dispatchEvent(new Event('input',{bubbles:true}))",
    );
    await assert(
      "document.querySelector('.ov-chart-readout').textContent.startsWith('08:00')",
      'Chart supports exact hourly inspection',
    );
    await evaluate(
      "var select=document.querySelector('[aria-label=\"店铺贡献指标\"]');Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,'orders');select.dispatchEvent(new Event('change',{bubbles:true}))",
    );
    await assert(
      "document.querySelector('.ov-donut-wrap svg').getAttribute('aria-label').startsWith('成交订单')",
      'Contribution metric switches',
    );
    await evaluate("document.querySelector('.ov-rank-row').click()");
    await wait("document.querySelectorAll('.ov-store-table tbody tr').length===1");
    await assert(
      '!!document.querySelector(\'[aria-label="概览店铺"]\').value',
      'Ranking filters the entire overview to one shop',
    );
    await evaluate('location.reload()');
    await wait("document.querySelectorAll('.ov-store-table tbody tr').length===1");
    await assert(
      "localStorage.getItem('pdd-manager:auto-refresh')==='false'&&!!document.querySelector('[aria-label=\"概览店铺\"]').value",
      'Shop filter and auto-refresh preference persist',
    );
    await evaluate(
      "var select=document.querySelector('[aria-label=\"概览店铺\"]');Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,'');select.dispatchEvent(new Event('change',{bubbles:true}))",
    );
    await wait("document.querySelectorAll('.ov-store-table tbody tr').length===2");
    await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
    await pause(250);
    await assert('document.documentElement.scrollWidth<=innerWidth+1', 'Mobile has no page overflow');
    await capture('overview-mobile.png');
    await evaluate("document.querySelector('.ov-middle-grid').scrollIntoView({block:'start'})");
    await capture('overview-mobile-charts.png');
    await call('Emulation.setDeviceMetricsOverride', { width: 1024, height: 900, deviceScaleFactor: 1, mobile: false });
    await pause(250);
    await assert('document.documentElement.scrollWidth<=innerWidth+1', 'Tablet has no page overflow');
    await call('Emulation.setDeviceMetricsOverride', {
      width: 1560,
      height: 1100,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await click('交易数据 ↗');
    await wait("document.querySelector('.business-heading h2')?.textContent==='交易数据'");
    await click('实时概览');
    await wait("!!document.querySelector('.overview-dashboard')");
    await click('营销活动');
    await wait("document.querySelector('.business-heading h2')?.textContent==='活动报名记录'");
    console.log('PASS Neighboring pages still work');
    await click('数据中心');
    await wait("document.querySelectorAll('.ov-store-table tbody tr').length===2");
    // Browser-only fixtures validate missing and zero values without changing saved business data.
    await evaluate(
      `window.__originalFetch=window.fetch;window.fetch=async function(input,init){if(String(input).startsWith('/api/data/overview'))return new Response(JSON.stringify({totals:{sales:0,orders:0,shipping_due:0},metrics:[{shop_id:1,shop_name:'缺失字段测试',sales:0,orders:0,updated_at:'2026-10-09T14:00:00+08:00',stale:true}],sources:[{shop_id:1,shop_name:'缺失字段测试',updated_at:'2026-10-09T14:00:00+08:00',error:'测试读取失败',stale:true,complete:true}]}),{status:200,headers:{'Content-Type':'application/json'}});return window.__originalFetch(input,init)};[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='刷新当前页面').click()`,
    );
    await wait("document.querySelector('.ov-revenue-value')?.textContent==='¥0.00'");
    await assert(
      "document.querySelectorAll('.ov-kpi strong')[0].textContent==='—'",
      'Missing visitor data is not represented as zero',
    );
    await assert(
      "!document.querySelector('.overview-dashboard').textContent.match(/NaN|Infinity/)",
      'Zero values do not create invalid chart numbers',
    );
    await evaluate(
      "window.fetch=window.__originalFetch;localStorage.setItem('pdd-manager:auto-refresh','false');location.reload()",
    );
    await wait("document.querySelectorAll('.ov-store-table tbody tr').length===2");
    if (errors.length) throw Error('Browser errors: ' + errors.join('\n'));
    console.log('Overview UI checks passed');
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
