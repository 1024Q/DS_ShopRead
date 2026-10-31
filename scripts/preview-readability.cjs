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
        `(()=>{const b=[...document.querySelectorAll('button')].find(e=>e.textContent.trim()===${JSON.stringify(text)});if(!b)throw Error('Missing button');b.click();})()`,
      );
      await pause(300);
    };
    await wait(
      "document.querySelectorAll('.main-tabs button').length===13&&document.querySelectorAll('.filter-panel select option').length===3",
    );
    await click('商家举证');
    await wait("document.querySelectorAll('.data-table tbody tr').length>0");
    console.log(
      'Desktop sizes',
      await evaluate(
        "Object.fromEntries(['.data-table','.data-table td small','.filter-panel input','.filter-panel>label','.main-tabs button'].map(s=>[s,getComputedStyle(document.querySelector(s)).fontSize]))",
      ),
    );
    const capture = async (file) => {
      const shot = await call('Page.captureScreenshot', { format: 'png' });
      fs.writeFileSync(path.join(root, '.data', file), Buffer.from(shot.data, 'base64'));
    };
    await capture('readability-desktop.png');
    await click('详情');
    await wait("!!document.querySelector('.record-dialog')");
    await capture('readability-details.png');
    await click('关闭');
    await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
    await pause(300);
    console.log(
      'Mobile overflow',
      await evaluate(
        "({viewport:innerWidth,page:document.documentElement.scrollWidth,filter:document.querySelector('.filter-panel').getBoundingClientRect().width})",
      ),
    );
    await capture('readability-mobile.png');
    await call('Emulation.clearDeviceMetricsOverride');
    await click('实时概览');
    await wait("document.querySelectorAll('.metric-card').length===12");
    await capture('readability-overview.png');
    console.log('Readability preview complete');
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
