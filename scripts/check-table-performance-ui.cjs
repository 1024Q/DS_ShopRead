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
      '--remote-debugging-port=19355',
      `--user-data-dir=${path.join(root, '.data/ui-table-performance')}`,
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19355/json/list')).json()).find((t) => t.type === 'page');
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
    const shops = [1, 2].map((id) => ({
      id,
      name: '性能验证店铺' + id,
      platform: 'pdd',
      operator: '测试运营',
      state: 'logged_in',
      username: 'fixture' + id,
      browser: 'system',
      visible: false,
    }));
    const sources = shops.map((s) => ({
      ...s,
      shop_id: s.id,
      shop_name: s.name,
      complete: true,
      updated_at: new Date().toISOString(),
    }));
    const row = (i, s = 1) => ({
      id: String(i),
      goods_id: String(100000 + i),
      shop_id: s,
      shop_name: shops[s - 1].name,
      platform: 'pdd',
      name: '商品' + String(i).padStart(4, '0'),
      stock: i,
      sales: 5000 - i,
      price_min: 12,
      price_max: 12,
      status: '在售中',
      created_at: '2026-10-09 10:00:00',
      kind: 'aftersales',
      priority: 3,
      problem: '待商家处理',
      order_number: 'ORDER' + i,
    });
    ws.addEventListener('message', async (e) => {
      const m = JSON.parse(e.data);
      if (m.method !== 'Fetch.requestPaused') return;
      const p = m.params,
        u = new URL(p.request.url);
      requests.push({ path: u.pathname, query: u.search, method: p.request.method });
      try {
        let data,
          delay = 0;
        if (u.pathname === '/api/shops') data = shops;
        else if (p.request.method === 'POST') data = { queued: 0 };
        else if (u.pathname === '/api/todos') data = { rows: Array.from({ length: 5000 }, (_, i) => row(i)), sources };
        else if (u.pathname.startsWith('/api/data/')) {
          let rows = Array.from({ length: 300 }, (_, i) => row(i, Number(u.searchParams.get('shop_id')) || 1));
          const field = u.searchParams.get('sort_by'),
            direction = u.searchParams.get('sort_direction');
          if (field)
            rows.sort(
              (a, b) =>
                (typeof a[field] === 'number'
                  ? a[field] - b[field]
                  : String(a[field]).localeCompare(String(b[field]))) * (direction === 'desc' ? -1 : 1),
            );
          const page = Number(u.searchParams.get('page')) || 1,
            size = Number(u.searchParams.get('page_size')) || 20;
          data = {
            rows: rows.slice((page - 1) * size, page * size),
            sources,
            total: rows.length,
            page_count: Math.ceil(rows.length / size),
            page,
            page_size: size,
            statuses: ['在售中'],
            metrics: [],
            scope: {},
          };
          delay = u.searchParams.get('shop_id') ? 900 : field === 'sales' ? 1000 : field ? 600 : 0;
        } else {
          await call('Fetch.continueRequest', { requestId: p.requestId });
          return;
        }
        if (delay) await pause(delay);
        await call('Fetch.fulfillRequest', {
          requestId: p.requestId,
          responseCode: 200,
          responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
          body: Buffer.from(JSON.stringify(data)).toString('base64'),
        });
      } catch (error) {
        if (
          !/Invalid InterceptionId|Invalid interception|No resource with given identifier|Session closed|WebSocket/i.test(
            error.message,
          )
        )
          errors.push(error.message);
      }
    });
    await call('Runtime.enable');
    await call('Page.enable');
    await call('Fetch.enable', {
      patterns: [{ urlPattern: '*/api/shops' }, { urlPattern: '*/api/data/*' }, { urlPattern: '*/api/todos*' }],
    });
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source:
        "if(location.origin==='http://127.0.0.1:3210'&&!sessionStorage.tableFixture){localStorage.clear();localStorage.setItem('pdd-manager:view','products');localStorage.setItem('pdd-manager:auto-refresh','false');localStorage.setItem('pdd-manager:filters:products',JSON.stringify({draft:{},pageSize:100}));sessionStorage.tableFixture='1';}",
    });
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await wait("!!document.querySelector('.virtual-table-viewport tbody tr[data-virtual-key]')", 60);
    await assert(
      "document.querySelectorAll('tr[data-virtual-key]').length<35&&document.querySelector('table[data-virtualized]').getAttribute('aria-rowcount')==='101'",
      '100-row page only mounts visible rows',
    );
    const sort = async (label) =>
      evaluate(
        `(()=>{const b=[...document.querySelectorAll('.column-sort')].find(b=>b.textContent===${JSON.stringify(label)});if(!b)throw Error('Missing sort '+${JSON.stringify(label)});b.click();})()`,
      );
    const beforePosts = requests.filter((r) => r.method === 'POST').length;
    await sort('累计销量');
    await pause(80);
    await assert(
      "document.querySelectorAll('tr[data-virtual-key]').length>0&&document.querySelector('.table-update-status').textContent.includes('保留')",
      'Rows stay visible during delayed server sorting',
    );
    await sort('库存');
    await wait("document.querySelector('.table-update-status').textContent===''");
    await pause(700);
    await assert(
      "document.querySelector('tr[data-virtual-key]').textContent.includes('商品0000')",
      'Rapid clicks accept only the latest sorting response',
    );
    if (requests.filter((r) => r.method === 'POST').length !== beforePosts)
      throw Error('Sorting registered repeated collection jobs');
    console.log('PASS sorting reuses collection demand');
    await evaluate("(()=>{const v=document.querySelector('.virtual-table-viewport');v.scrollTop=v.scrollHeight;})()");
    await wait("document.querySelector('tbody').textContent.includes('商品0099')");
    await assert(
      "document.querySelectorAll('tr[data-virtual-key]').length<35",
      'Virtual scrolling reaches the last row without mounting the whole page',
    );
    await evaluate("document.querySelector('.virtual-table-viewport').scrollTop=0");
    await pause(300);
    await wait("!!document.querySelector('.column-resizer')");
    await evaluate(
      "(()=>{const h=document.querySelector('.column-resizer');h.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));})()",
    );
    await assert(
      "!!JSON.parse(localStorage.getItem('pdd-manager:columns:products'))[Object.keys(JSON.parse(localStorage.getItem('pdd-manager:columns:products')))[0]][0].width",
      'Column width changes still persist',
    );
    await evaluate("document.querySelector('.column-settings summary').click()");
    await pause(200);
    await evaluate(
      "[...document.querySelectorAll('.column-checkboxes label')].find(l=>l.textContent==='库存').querySelector('input').click()",
    );
    await pause(200);
    await assert(
      "[...document.querySelectorAll('thead th')].find(h=>h.textContent==='库存').hidden",
      'Column visibility works inside virtual tables',
    );
    await assert(
      "[...document.querySelectorAll('thead th')].filter(h=>!h.hidden).every(h=>h.getBoundingClientRect().width>20)",
      'Hiding one column leaves every other column visible',
    );
    await evaluate("document.body.click();document.querySelector('.virtual-table-viewport').scrollTop=5000");
    await pause(300);
    await assert(
      "[...document.querySelectorAll('tr[data-virtual-key]')].every(r=>r.cells[4].hidden)",
      'Newly mounted rows inherit hidden columns',
    );
    await evaluate(
      "(()=>{const s=document.querySelector('.filter-panel select');s.value='2';s.dispatchEvent(new Event('change',{bubbles:true}));})()",
    );
    await pause(550);
    await assert(
      "!document.querySelector('tbody').textContent.includes('性能验证店铺1')",
      'Changing shops does not display the previous shop under new filters',
    );
    await wait("document.querySelector('tbody').textContent.includes('性能验证店铺2')");
    await evaluate("localStorage.setItem('pdd-manager:view','todos');location.reload()");
    await wait("document.querySelector('table[data-virtualized]')?.getAttribute('aria-rowcount')==='5001'");
    await sort('商品');
    await pause(100);
    await sort('商品');
    await pause(250);
    await assert(
      "document.querySelector('tr[data-virtual-key]').textContent.includes('商品4999')",
      '5,000-row local list sorts the entire result',
    );
    await assert(
      "document.querySelectorAll('tr[data-virtual-key]').length<35",
      '5,000 rows keep a bounded number of DOM rows',
    );
    await evaluate("(()=>{const v=document.querySelector('.virtual-table-viewport');v.scrollTop=v.scrollHeight;})()");
    await wait("document.querySelector('tbody').textContent.includes('商品0000')");
    await evaluate(
      "document.querySelector('.virtual-table-viewport').scrollTop=0;document.documentElement.dataset.theme='dark'",
    );
    await pause(300);
    fs.writeFileSync(
      path.join(root, '.data/table-performance-dark.png'),
      Buffer.from((await call('Page.captureScreenshot', { format: 'png' })).data, 'base64'),
    );
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL table performance UI checks passed');
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
