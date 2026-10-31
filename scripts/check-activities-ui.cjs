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
      '--remote-debugging-port=19323',
      `--user-data-dir=${path.join(root, '.data/ui-activities-test')}`,
      'http://127.0.0.1:3210/',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19323/json/list')).json()).find((t) => t.type === 'page');
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
      "localStorage.setItem('pdd-manager:auto-refresh','false');localStorage.setItem('pdd-manager:view','overview');localStorage.removeItem('pdd-manager:filters:activities');location.reload()",
    );
    await wait("!!document.querySelector('.workspace-sidebar')");
    await click('营销活动');
    await wait(
      "document.querySelector('.business-heading h2')?.textContent==='活动报名记录'&&document.querySelectorAll('.custom-table tbody tr').length>0",
      90,
    );
    await assert("!document.querySelector('.source-chip')", 'Source cards centralized outside the activity page');
    await assert(
      "document.querySelector('.data-note').textContent.includes('不包含已归档记录')",
      'Recent and archived scope clearly separated',
    );
    await wait("[...document.querySelectorAll('.thumbnail-button img')].some(i=>i.complete&&i.naturalWidth>0)");
    const data = await evaluate(
      "(async()=>{const r=await fetch('/api/data/activities');const d=await r.json();window.__sample=d.rows[0];return {total:d.total,types:d.activity_types,sources:d.sources.map(s=>({id:s.shop_id,count:s.loaded_count,total:s.source_total,complete:s.complete,error:s.error}))};})()",
    );
    console.log('Activity sources', data);
    if (data.sources.some((s) => s.error || !s.complete)) throw Error('Activity source incomplete');
    await evaluate(
      "var sel=document.querySelector('select[aria-label=\"活动类型\"]');Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(sel,window.__sample.activity_type);sel.dispatchEvent(new Event('change',{bubbles:true}));var input=document.querySelector('.keyword-filter input');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,window.__sample.activity_id);input.dispatchEvent(new Event('input',{bubbles:true}));",
    );
    await pause(800);
    await wait("document.querySelectorAll('.custom-table tbody tr').length>0");
    await assert(
      "[...document.querySelectorAll('.custom-table tbody tr')].every(r=>r.innerText.includes(window.__sample.activity_type)&&r.innerText.includes(window.__sample.activity_id))",
      'Activity type and ID auto filters work',
    );
    await evaluate("document.querySelector('.custom-table').scrollIntoView({block:'start'})");
    await capture('activities-list.png');
    await click('详情');
    await wait("!!document.querySelector('.activity-detail')");
    await assert(
      "document.querySelector('.activity-detail').innerText.includes('活动排期与价格')&&document.querySelector('.activity-detail').innerText.includes('报名规格价格')",
      'Enrollment details show schedules and SKU prices',
    );
    await capture('activities-detail.png');
    await click('关闭');
    await evaluate('location.reload()');
    await wait("document.querySelectorAll('.custom-table tbody tr').length>0");
    await assert(
      "document.querySelector('.business-heading h2').textContent==='活动报名记录'&&!!document.querySelector('.keyword-filter input').value&&!!document.querySelector('select[aria-label=\"活动类型\"]').value",
      'Activity view and filters survive reload',
    );
    const exportInfo = await evaluate(
      "(async()=>{const p=new URLSearchParams({export:true,activity_type:document.querySelector('select[aria-label=\"活动类型\"]').value,keyword:document.querySelector('.keyword-filter input').value});const r=await fetch('/api/data/activities?'+p);const b=new Uint8Array(await r.arrayBuffer());return {status:r.status,zip:b[0]===80&&b[1]===75,bytes:b.length};})()",
    );
    if (exportInfo.status !== 200 || !exportInfo.zip) throw Error('Export failed');
    console.log('PASS Activity Excel export', exportInfo);
    await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
    await pause(250);
    await assert('document.documentElement.scrollWidth<=innerWidth+1', 'Activity mobile layout fits viewport');
    await capture('activities-mobile.png');
    if (errors.length) throw Error('Browser errors: ' + errors.join('\n'));
    console.log('Activities UI checks passed');
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
