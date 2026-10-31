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
      '--remote-debugging-port=19325',
      `--user-data-dir=${path.join(root, '.data/ui-shell-test')}`,
      'http://127.0.0.1:3210/',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19325/json/list')).json()).find((t) => t.type === 'page');
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
    await evaluate(
      "localStorage.setItem('pdd-manager:auto-refresh','false');localStorage.setItem('pdd-manager:view','overview');localStorage.removeItem('pdd-manager:filters:overview');location.reload()",
    );
    await wait("document.querySelectorAll('.ov-store-table tbody tr').length>0");
    await assert("document.title==='多店铺数据工作台'", 'Brand and browser title updated');
    await assert(
      "!document.querySelector('.live-log-strip,.source-list,.ov-auto,.refresh-actions input')",
      'Overview does not contain per-page logs, sources or refresh checkbox',
    );
    await wait("document.querySelector('.global-sync-info strong')?.textContent!=='读取同步状态…'");
    await assert("!!document.querySelector('.global-sync-bar')", 'Unified synchronization toolbar is visible');
    await evaluate(
      'window.__calls=[];window.__fetch=window.fetch;window.fetch=function(input,init){window.__calls.push(String(input));return window.__fetch(input,init)}',
    );
    await click('刷新当前页面');
    await wait("window.__calls.some(u=>u.startsWith('/api/data/overview?')&&u.includes('force=true'))");
    await assert(
      "window.__calls.some(u=>u.startsWith('/api/data/transactions?')&&u.includes('force=true'))",
      'Global refresh updates overview and trade chart',
    );
    await capture('shell-overview-desktop.png');
    await click('系统管理');
    await click('批量导入店铺');
    await wait("!!document.querySelector('.import-dialog')");
    const setText = async (value) =>
      evaluate(
        `(()=>{const e=document.querySelector('[aria-label="店铺导入内容"]');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event('input',{bubbles:true}));})()`,
      );
    await setText('账号\t密码\t店铺名称\nui-preview-only\tDummy-Password-Never-Saved\t预览店铺\nbroken\t\t错误行');
    await click('预览并校验');
    await wait("!!document.querySelector('.import-result')");
    await assert(
      "document.querySelectorAll('.import-invalid').length===1&&[...document.querySelectorAll('.import-footer button')].find(b=>b.textContent.startsWith('确认导入')).disabled",
      'Invalid rows prevent commit',
    );
    await setText('账号,密码,店铺名称,浏览器\nui-preview-only,Dummy-Password-Never-Saved,预览店铺,bundled');
    await click('预览并校验');
    await wait("document.querySelector('.import-result')?.textContent.includes('错误 0 行')");
    await assert(
      "!document.querySelector('.import-preview').textContent.includes('Dummy-Password')&&document.querySelector('.import-preview').textContent.includes('项目 Chrome')",
      'Preview masks credentials and shows selected browser',
    );
    await assert(
      "![...document.querySelectorAll('.import-footer button')].find(b=>b.textContent.startsWith('确认导入')).disabled",
      'Validated batch is ready for explicit import',
    );
    await capture('shell-import-preview.png');
    const shopCount = await evaluate(
      "(async()=>{const s=await(await fetch('/api/shops')).json();window.__existingAccount=s[0].username;return s.length;})()",
    );
    await evaluate(
      "var el=document.querySelector('[aria-label=\"店铺导入内容\"]');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(el,'账号,密码'+String.fromCharCode(10)+window.__existingAccount+',dummy');el.dispatchEvent(new Event('input',{bubbles:true}));",
    );
    await click('预览并校验');
    await wait("document.querySelector('.import-result')?.textContent.includes('跳过 1 家')");
    await assert(
      "document.querySelector('.import-preview').textContent.includes('账号已存在，跳过')",
      'Existing accounts are skipped',
    );
    await evaluate('document.querySelector(\'[aria-label="关闭批量导入"]\').click()');
    await click('批量导入店铺');
    await assert(
      "document.querySelector('[aria-label=\"店铺导入内容\"]').value===''",
      'Import credentials cleared when dialog closes',
    );
    const uploadPath = path.join(root, '.data', 'ui-batch-upload.csv');
    fs.writeFileSync(
      uploadPath,
      `\ufeff店铺名称,账号,密码,浏览器\r\n"上传,预览店",upload-preview-only,Only-A-Test-Password,system\r\n`,
    );
    try {
      const doc = await call('DOM.getDocument');
      const node = await call('DOM.querySelector', {
        nodeId: doc.root.nodeId,
        selector: '.import-dialog input[type=file]',
      });
      await call('DOM.setFileInputFiles', { nodeId: node.nodeId, files: [uploadPath] });
      await wait("document.querySelector('[aria-label=\"店铺导入内容\"]').value.includes('upload-preview-only')");
      await click('预览并校验');
      await wait("document.querySelector('.import-result')?.textContent.includes('错误 0 行')");
      await assert(
        "document.querySelector('.import-preview').textContent.includes('上传,预览店')",
        'CSV file upload supports BOM, reordered columns and quoted commas',
      );
    } finally {
      fs.unlinkSync(uploadPath);
    }

    await evaluate('document.querySelector(\'[aria-label="关闭批量导入"]\').click()');
    const after = await evaluate("(async()=>{const s=await(await fetch('/api/shops')).json();return s.length;})()");
    if (after !== shopCount) throw Error('Preview altered shop database');
    console.log('PASS Browser preview does not write to database');
    await click('同步与数据源');
    await wait("document.querySelectorAll('.source-chip').length>0");
    await evaluate(
      "var select=document.querySelector('[aria-label=\"数据源模块\"]');Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,'overview');select.dispatchEvent(new Event('change',{bubbles:true}));",
    );
    await assert("document.querySelectorAll('.source-chip').length===2", 'System source center filters by module');
    await capture('shell-sync-center.png');
    await click('系统设置');
    await wait('!!document.querySelector(\'[aria-label="全局自动刷新"]\')');
    await evaluate(
      "var select=document.querySelector('[aria-label=\"全局自动刷新\"]');Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,'60');select.dispatchEvent(new Event('change',{bubbles:true}));",
    );
    await assert(
      "document.querySelector('.global-auto-label').textContent.includes('每 60 秒')&&localStorage.getItem('pdd-manager:auto-refresh')==='true'",
      'Global refresh setting updates and persists',
    );
    await evaluate('location.reload()');
    await wait("!!document.querySelector('.settings-page')");
    await assert(
      "document.querySelector('[aria-label=\"全局自动刷新\"]').value==='60'",
      'Global setting survives reload',
    );
    await evaluate(
      "var select=document.querySelector('[aria-label=\"全局自动刷新\"]');Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(select,'off');select.dispatchEvent(new Event('change',{bubbles:true}));",
    );
    await click('运行日志');
    await wait("!!document.querySelector('.log-table')");
    await assert(
      "!document.querySelector('.source-list,.live-log-strip')",
      'Logs are exclusively in the system log page',
    );
    await click('营销活动');
    await wait("document.querySelector('.business-heading h2')?.textContent==='活动报名记录'");
    await assert(
      "!document.querySelector('.source-list,.live-log-strip,.refresh-actions input')",
      'Activity page uses unified controls',
    );
    await capture('shell-activities.png');
    await click('商品评价');
    await wait("document.querySelector('.business-heading h2')?.textContent==='商品列表'");
    await click('评价管理');
    await wait("!!document.querySelector('.star-filter')");
    await assert(
      "document.querySelectorAll('.star-filter input[type=checkbox]').length===5",
      'Business filter checkboxes remain intact',
    );
    await click('资金报税');
    await click('报税信息');
    await wait(
      '!!document.querySelector(\'[aria-label="入账月份"]\')||!!document.querySelector(\'[aria-label="入账季度"]\')',
    );
    await assert("!document.querySelector('.source-list,.live-log-strip')", 'Tax page excludes source and log panels');
    await click('订单售后');
    await wait("document.querySelector('.business-heading h2')?.textContent==='重点待办中心'");
    await assert("!document.querySelector('.source-list,.live-log-strip')", 'Todo page uses centralized status');
    await click('数据中心');
    await wait("!!document.querySelector('.overview-dashboard')");
    await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
    await pause(300);
    await assert('document.documentElement.scrollWidth<=innerWidth+1', 'Overview mobile layout fits viewport');
    await evaluate('window.scrollTo(0,0)');
    await capture('shell-mobile.png');
    await click('系统管理');
    await click('批量导入店铺');
    await assert(
      "document.querySelector('.import-dialog').getBoundingClientRect().width<=innerWidth",
      'Import dialog fits mobile',
    );
    await capture('shell-import-mobile.png');
    await evaluate('document.querySelector(\'[aria-label="关闭批量导入"]\').click()');
    await click('同步与数据源');
    await wait("!!document.querySelector('.sync-center')");
    await assert('document.documentElement.scrollWidth<=innerWidth+1', 'Source center mobile fits viewport');
    if (errors.length) throw Error('Browser errors: ' + errors.join('\n'));
    console.log('Unified workspace UI checks passed');
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
