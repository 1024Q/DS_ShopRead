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
      for (let n = 0; n < 180; n++) {
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
      "document.querySelectorAll('.main-tabs button').length===13&&document.querySelectorAll('.data-table tbody tr').length===2",
    );
    await click('商家举证');
    await wait("document.querySelectorAll('.priority-row').length>0");
    if (!(await evaluate("document.querySelector('.data-table tbody tr').classList.contains('priority-row')")))
      throw Error('Priority evidence is not first');
    await evaluate("document.querySelector('.priority-filter input').click()");
    await click('查询');
    await wait(
      "document.querySelectorAll('.data-table tbody tr').length>0&&[...document.querySelectorAll('.data-table tbody tr')].every(e=>e.classList.contains('priority-row'))",
    );
    let shot = await call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(root, 'business-operations-preview.png'), Buffer.from(shot.data, 'base64'));
    await click('详情');
    await wait("!!document.querySelector('.record-dialog')");
    if (!(await evaluate("document.querySelector('.record-dialog').innerText.includes('订单编号')")))
      throw Error('Evidence details missing');
    await click('关闭');
    for (const title of ['售后工作台', '包裹中心', '订单开票', '商品领航员', '品质诊断']) {
      await click(title);
      await wait("document.querySelectorAll('.data-table tbody tr').length>0");
      console.log('PASS page: ' + title);
    }
    await click('拦截快递');
    await wait("document.querySelectorAll('.identifier-cell').length>0");
    if (!(await evaluate("document.querySelector('.data-table').innerText.includes('快递 / 运单号')")))
      throw Error('Tracking column missing');
    await click('品牌黑标权益');
    await wait("document.querySelectorAll('.data-table tbody tr').length>0");
    if (
      !(await evaluate(
        "[...document.querySelectorAll('.data-table tbody tr')].every(e=>e.innerText.includes('无黑标商品'))",
      ))
    )
      throw Error('Brand scope incorrect');
    await wait("document.querySelector('.source-error')?.innerText.includes('权限')");
    shot = await call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(root, 'business-brands-preview.png'), Buffer.from(shot.data, 'base64'));
    await click('工单列表');
    await wait("document.querySelectorAll('.data-table tbody tr').length>0");
    if (
      !(await evaluate(
        "[...document.querySelectorAll('.data-table tbody tr')].filter(e=>e.innerText.includes('违规已处理')).every(e=>e.classList.contains('priority-row'))",
      ))
    )
      throw Error('Ticket highlight missing');
    await click('店铺管理');
    await wait("[...document.querySelectorAll('button')].filter(e=>e.textContent==='编辑店铺').length===2");
    console.log(
      'PASS: all new modules, evidence highlights/filter, details, parcel identifiers, black-label scope, permission errors, tickets and shop management.',
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
