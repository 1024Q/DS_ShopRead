const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
(async () => {
  const root = path.resolve(__dirname, '..');
  const chrome = spawn(
    path.join(root, '.browsers/chrome-win64/chrome.exe'),
    [
      '--headless=new',
      '--no-first-run',
      '--window-size=1440,1100',
      '--remote-debugging-port=19321',
      `--user-data-dir=${path.join(root, '.data/ui-business-test')}`,
      'http://127.0.0.1:3210/',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 30; n++) {
      try {
        const list = await (await fetch('http://127.0.0.1:19321/json/list')).json();
        target = list.find((t) => t.type === 'page');
        if (target) break;
      } catch {}
      await pause(300);
    }
    if (!target) throw Error('Test browser did not start');
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve, { once: true });
      ws.addEventListener('error', reject, { once: true });
    });
    let id = 0;
    const pending = new Map();
    ws.addEventListener('message', (event) => {
      const m = JSON.parse(event.data);
      const p = pending.get(m.id);
      if (p) {
        pending.delete(m.id);
        m.error ? p.reject(Error(m.error.message)) : p.resolve(m.result);
      }
    });
    call = (method, params = {}) =>
      new Promise((resolve, reject) => {
        const n = ++id;
        pending.set(n, { resolve, reject });
        ws.send(JSON.stringify({ id: n, method, params }));
      });
    const evaluate = async (expression) => {
      const r = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw Error(r.exceptionDetails.text);
      return r.result.value;
    };
    const wait = async (expression) => {
      for (let n = 0; n < 60; n++) {
        if (await evaluate(expression)) return;
        await pause(1000);
      }
      console.log(
        await evaluate(
          "({url:location.href,text:document.body.innerText.slice(0,3000),metrics:document.querySelectorAll('.metric-card').length,rows:document.querySelectorAll('.data-table tbody tr').length})",
        ),
      );
      throw Error('Timed out: ' + expression);
    };
    const click = async (text) => {
      await evaluate(
        `(()=>{const b=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()===${JSON.stringify(text)}||e.getAttribute('aria-label')===${JSON.stringify(text)});if(!b)throw Error('Missing button');b.click();})()`,
      );
      await pause(300);
    };
    await wait(
      "document.querySelectorAll('.metric-card').length===12&&document.querySelectorAll('.data-table tbody tr').length===2",
    );
    let shot = await call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(root, 'business-overview-preview.png'), Buffer.from(shot.data, 'base64'));
    await click('商品列表');
    await wait(
      "document.querySelectorAll('.data-table tbody tr').length>0&&!document.body.innerText.includes('正在读取商家后台')",
    );
    if (!(await evaluate("document.body.innerText.includes('匹配 221 条')")))
      throw Error('Product aggregate count incorrect');
    await click('下一页');
    await wait("document.querySelector('.pagination')?.innerText.includes('第 2 /')");
    await click('工单列表');
    await wait("document.querySelectorAll('.data-table tbody tr').length>0");
    await click('评价管理');
    await evaluate("[...document.querySelectorAll('.star-filter input')].slice(0,3).forEach(e=>e.click())");
    await evaluate(
      "(()=>{const select=[...document.querySelectorAll('.filter-panel select')].find(e=>e.querySelector('option[value=unreplied]'));Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,'unreplied');select.dispatchEvent(new Event('change',{bubbles:true}));})()",
    );
    await pause(300);
    await click('查询');
    await wait("document.querySelectorAll('.rating').length>0&&!document.body.innerText.includes('正在读取商家后台')");
    if (
      !(await evaluate(
        "[...document.querySelectorAll('.rating')].every(e=>parseInt(e.textContent)<=3)&&[...document.querySelectorAll('.data-table tbody tr')].every(e=>e.innerText.includes('未回复'))",
      ))
    )
      throw Error('Review filters incorrect');
    await click('详情');
    await wait("!!document.querySelector('.record-dialog')");
    await click('关闭');
    shot = await call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(root, 'business-reviews-preview.png'), Buffer.from(shot.data, 'base64'));
    await click('店铺管理');
    await wait("[...document.querySelectorAll('button')].filter(e=>e.textContent==='编辑店铺').length===2");
    console.log(
      'PASS: overview, multi-shop products (221), pagination, tickets, 1–3 star unreplied filters, details, and shop management.',
    );
  } finally {
    if (call) {
      await call('Browser.close').catch(() => {});
      ws.close();
    } else chrome.kill();
  }
})().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
