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

    const commands = [];
    const row = {
      id: 'fixture-1',
      goods_id: '998877665544332211',
      order_number: '20261010-123456789012345',
      shop_id: 5,
      shop_name: '拼多多测试店铺',
      platform: 'pdd',
      name: '长标题验证商品完整信息'.repeat(25),
      category: '分类完整信息'.repeat(15),
      thumbnail_url: 'https://example.test/product.png',
      status: '在售中',
      stars: 1,
      content: '评价全文验证内容'.repeat(100),
      reply: '商家回复全文内容'.repeat(100),
      replied: true,
      created_at: '2026-09-01 10:00:00',
      stock: 100,
      sales: 999,
      sales_30d: 222,
      price_min: 10,
      price_max: 20,
    };
    const fixture = () => ({
      rows: [row, { ...row, id: 'fixture-2', name: '短标题商品' }],
      total: 2,
      page: 1,
      page_count: 1,
      priority_count: 0,
      statuses: ['在售中'],
      sources: [
        { shop_id: 5, platform: 'pdd', shop_name: '测试店铺', updated_at: new Date().toISOString(), complete: true },
      ],
      metrics: [],
      scope: {},
    });
    ws.addEventListener('message', async (e) => {
      const m = JSON.parse(e.data);
      if (m.method !== 'Fetch.requestPaused') return;
      const p = m.params;
      try {
        if (p.request.url.startsWith('https://example.test/')) {
          await call('Fetch.fulfillRequest', {
            requestId: p.requestId,
            responseCode: 200,
            responseHeaders: [{ name: 'Content-Type', value: 'image/png' }],
            body: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aQmcAAAAASUVORK5CYII=',
          });
          return;
        }
        requests.push(p.request.url);
        if (p.request.method === 'POST') commands.push(p.request.url);
        await call('Fetch.fulfillRequest', {
          requestId: p.requestId,
          responseCode: 200,
          responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
          body: Buffer.from(
            JSON.stringify(
              p.request.method === 'POST' ? { tasks: [{ shop_id: 5, syncing: true, cached: false }] } : fixture(),
            ),
          ).toString('base64'),
        });
      } catch (e) {
        errors.push(e.message);
      }
    });
    await call('Runtime.enable');
    await call('Page.enable');
    await call('Fetch.enable', {
      patterns: [
        { urlPattern: '*/api/data/reviews*' },
        { urlPattern: '*/api/data/products*' },
        { urlPattern: 'https://example.test/*' },
      ],
    });
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source: `if(location.origin==='http://127.0.0.1:3210'){localStorage.clear();sessionStorage.clear();localStorage.setItem('pdd-manager:view','reviews');localStorage.setItem('pdd-manager:auto-refresh','false');}`,
    });
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait(
      "document.querySelector('.business-heading h2')?.textContent==='评价管理'&&document.querySelectorAll('.business-panel .data-table tbody tr').length===2",
    );
    await assert(
      "!!document.querySelector('button[aria-label=商品管理]')&&!document.querySelector('button[aria-label=商品评价]')",
      'navigation renamed',
    );
    const checkRows = async (label) => {
      await pause(500);
      const sizes = await evaluate(
        "Array.from(document.querySelectorAll('.business-panel .data-table tbody tr')).map(r=>Math.round(r.getBoundingClientRect().height))",
      );
      console.log(label, 'row heights', sizes);
      if (sizes.some((h) => h > 100)) throw Error(label + ' row too tall');
      await assert(
        "getComputedStyle(document.querySelector('.goods-thumbnail')).width==='40px'",
        label + ' 40px thumbnails',
      );
    };
    await checkRows('reviews');
    await assert("document.querySelector('.clamp-text').title.length>500", 'full review available on hover');
    await evaluate("document.querySelector('.thumbnail-button').click()");
    await wait("!!document.querySelector('.image-preview')");
    await evaluate("document.querySelector('.image-preview button').click()");
    await evaluate(
      "Array.from(document.querySelectorAll('.business-panel .table-action')).find(b=>b.textContent==='详情').click()",
    );
    await wait("!!document.querySelector('.record-dialog')");
    await assert(
      "document.querySelector('.record-dialog').textContent.includes('评价全文验证内容'.repeat(50))",
      'detail preserves full review',
    );
    await evaluate("document.querySelector('.record-dialog .dialog-head button').click()");
    const shot = await call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(root, '.data/compact-reviews-ui.png'), Buffer.from(shot.data, 'base64'));
    await evaluate("Array.from(document.querySelectorAll('button')).find(b=>b.textContent==='采集指定日期').click()");
    await wait("!!document.querySelector('.date-collection-dialog')");
    await evaluate(
      "(()=>{const box=document.querySelector('.date-collection-dialog');const select=box.querySelectorAll('select')[1];Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,'5');select.dispatchEvent(new Event('change',{bubbles:true}));for(const [i,input] of Array.from(box.querySelectorAll('input[type=date]')).entries()){Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,i?'2026-09-02':'2026-09-01');input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));}})()",
    );
    await evaluate("document.querySelector('.date-collection-dialog button[type=submit]').click()");
    await wait(
      "!document.querySelector('.date-collection-dialog')&&document.querySelector('.business-heading h2')?.textContent==='评价管理'",
    );
    await pause(800);
    if (commands.length !== 1) throw Error('Expected one explicit collection command, got ' + commands.length);
    const request = new URL(commands[0]);
    for (const [key, value] of Object.entries({
      date_from: '2026-09-01',
      date_to: '2026-09-02',
      exact_dates: 'true',
      force: 'true',
      shop_id: '5',
      platform: 'pdd',
    }))
      if (request.searchParams.get(key) !== value) throw Error('wrong ' + key + ': ' + request.searchParams.get(key));
    if (
      !requests.some((u) => {
        const q = new URL(u);
        return (
          !u.includes('/sync') &&
          q.searchParams.get('exact_dates') === 'true' &&
          q.searchParams.get('date_from') === '2026-09-01'
        );
      })
    )
      throw Error('No exact-date GET after collection');
    console.log('PASS one date collection command and matching database query');
    await evaluate(
      "Array.from(document.querySelectorAll('.main-tabs button')).find(b=>b.textContent==='商品列表').click()",
    );
    await wait(
      "document.querySelector('.business-heading h2')?.textContent==='商品列表'&&document.querySelectorAll('.business-panel .data-table tbody tr').length===2",
    );
    await checkRows('products');
    for (const theme of ['white', 'dark', 'blue']) {
      await evaluate(`document.documentElement.dataset.theme='${theme}'`);
      await checkRows('products ' + theme);
    }
    const productShot = await call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(root, '.data/compact-products-ui.png'), Buffer.from(productShot.data, 'base64'));
    await evaluate("document.querySelector('button[aria-label=系统管理]').click()");
    await wait("Array.from(document.querySelectorAll('.main-tabs button')).some(b=>b.textContent==='同步与数据源')");
    await evaluate(
      "Array.from(document.querySelectorAll('.main-tabs button')).find(b=>b.textContent==='同步与数据源').click()",
    );
    await wait("!!document.querySelector('.sync-center .date-collection-control button')");
    await evaluate("document.querySelector('.sync-center .date-collection-control button').click()");
    await wait("!!document.querySelector('.date-collection-dialog')");
    await assert(
      "document.querySelector('.date-collection-dialog select').options.length===11&&!document.querySelector('.date-collection-dialog select').disabled",
      'system center offers all supported modules and accounting periods',
    );
    await evaluate("document.querySelector('.date-collection-dialog .dialog-head button').click()");
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL COMPACT TABLE AND DATE COLLECTION CHECKS PASSED');
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
