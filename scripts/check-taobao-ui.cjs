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
      '--remote-debugging-port=19353',
      `--user-data-dir=${path.join(root, '.data/ui-taobao')}`,
      'http://127.0.0.1:3210/',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let i = 0; i < 40; i++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19353/json/list')).json()).find((t) => t.type === 'page');
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
      if (m.method === 'Network.requestWillBeSent') requests.push(m.params.request.url);
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
    const wait = async (expression, seconds = 45) => {
      for (let i = 0; i < seconds * 2; i++) {
        if (await evaluate(expression)) return;
        await pause(500);
      }
      throw Error('Timed out: ' + expression + '\n' + (await evaluate('document.body.innerText.slice(0,2000)')));
    };
    const assert = async (expression, label) => {
      if (!(await evaluate(expression))) throw Error(label);
      console.log('PASS', label);
    };
    const capture = async (name) => {
      await evaluate("[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='关闭画面')?.click()");
      await pause(300);
      fs.writeFileSync(
        path.join(root, '.data', name),
        Buffer.from((await call('Page.captureScreenshot', { format: 'png' })).data, 'base64'),
      );
    };
    await call('Runtime.enable');
    await call('Network.enable');
    await call('Page.enable');
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait("!!document.querySelector('.workspace-sidebar')");
    const sid = await evaluate("fetch('/api/shops').then(r=>r.json()).then(s=>s.find(x=>x.platform==='taobao').id)");
    await evaluate(
      "localStorage.setItem('pdd-manager:view','overview');localStorage.setItem('pdd-manager:auto-refresh','false');location.reload()",
    );
    await wait(
      `!!document.querySelector('[aria-label="概览店铺"] option[value="${sid}"]')&&document.querySelectorAll('.ov-store-table tbody tr').length>1`,
    );
    await pause(500);
    await evaluate(
      `(()=>{const e=document.querySelector('[aria-label="概览店铺"]');e.value='${sid}';e.dispatchEvent(new Event('change',{bubbles:true}));})()`,
    );
    await wait(
      "document.querySelectorAll('.ov-store-table tbody tr').length===1&&document.querySelector('.ov-revenue-value')?.textContent!=='—'&&!!document.querySelector('.shop-comparison-chart')",
    );
    await assert(
      "document.querySelector('.ov-store-table tbody tr').textContent.includes('淘宝')&&!document.querySelector('.ov-store-table tbody tr').textContent.includes('业务数据待接入')",
      'Taobao overview and today/yesterday chart use saved data',
    );
    requests.length = 0;
    await pause(1500);
    if (requests.some((u) => u.includes('/api/data/products') && u.includes('shop_id=' + sid)))
      throw Error('Unsupported Taobao module requested');
    console.log('PASS no unsupported Taobao product catalog requests');
    await capture('taobao-overview.png');
    await evaluate(
      "[...document.querySelectorAll('.shop-comparison button')].find(e=>e.textContent.includes('交易数据'))?.click()",
    );
    // The card's accessible control is the entire card in compact layouts.
    if (!(await evaluate("!!document.querySelector('.transaction-data')")))
      await evaluate(
        "[...document.querySelectorAll('button')].find(e=>e.getAttribute('aria-label')?.includes('查看淘宝')&&e.getAttribute('aria-label')?.includes('交易数据'))?.click()",
      );
    await wait("!!document.querySelector('.transaction-data .trade-detail-heading')");
    await assert(
      "document.querySelector('[aria-label=\"业务平台筛选\"]').value==='taobao'&&document.querySelector('.transaction-data').textContent.includes('支付子订单')",
      'Taobao transaction drilldown keeps platform and metric definitions',
    );
    await capture('taobao-transactions.png');
    await evaluate(
      "localStorage.setItem('pdd-manager:view','goodsdata');localStorage.setItem('pdd-manager:filters:goodsdata',JSON.stringify({draft:{shop_id:'" +
        sid +
        "',trade_range:'yesterday'}}));location.reload()",
    );
    await wait("document.querySelector('.goods-analytics')?.textContent.includes('匹配 74 件')");
    await assert(
      "!!document.querySelector('.goods-analytics .goods-with-thumbnail img')&&document.querySelector('.goods-analytics').textContent.includes('商品排行')",
      'Taobao historical goods include photos and scope note',
    );
    await evaluate("document.querySelector('.goods-analytics .table-caption').scrollIntoView({block:'start'})");
    await capture('taobao-goods.png');
    await evaluate(
      "[...document.querySelectorAll('.goods-analytics .table-action')].find(e=>e.textContent==='详情').click()",
    );
    await wait('!!document.querySelector(\'[aria-label="商品数据详情"]\')');
    await assert(
      "document.querySelector('[aria-label=\"商品数据详情\"]').textContent.includes('退款金额')",
      'Goods detail renders refund and comparisons',
    );
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL Taobao UI checks passed');
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
