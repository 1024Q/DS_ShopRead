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
      '--remote-debugging-port=19321',
      `--user-data-dir=${path.join(root, '.data/ui-workspace-test')}`,
      'http://127.0.0.1:3210/',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let ws, call;
  try {
    let target;
    for (let n = 0; n < 40; n++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19321/json/list')).json()).find((t) => t.type === 'page');
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
      "localStorage.setItem('pdd-manager:auto-refresh','false');localStorage.setItem('pdd-manager:view','products');localStorage.removeItem('pdd-manager:filters:products');localStorage.removeItem('pdd-manager:columns:products');localStorage.removeItem('pdd-manager:presets:products');location.reload()",
    );
    await wait(
      "document.querySelector('.business-heading h2')?.textContent==='商品列表'&&document.querySelectorAll('.custom-table tbody tr').length>0",
      90,
    );
    await assert(
      "document.querySelector('.custom-table th').querySelector('.column-resizer')!==null",
      'Resizable sticky columns available',
    );
    await assert(
      "document.querySelector('.column-settings summary').getBoundingClientRect().width<200&&!document.querySelector('.column-settings').open",
      'Table settings is a compact collapsed button',
    );
    await evaluate(
      "document.querySelector('.column-settings').open=true; var label=[...document.querySelectorAll('.column-checkboxes label')].find(e=>e.textContent==='累计销量');label.querySelector('input').click();",
    );
    await assert(
      "[...document.querySelectorAll('.custom-table th')].find(e=>e.textContent==='累计销量').hidden",
      'Hide column',
    );
    await evaluate(
      "document.querySelector('.column-resizer').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}))",
    );
    await assert("!!document.querySelector('.custom-table th').style.width", 'Resize column persists');
    const drag = await evaluate(
      "(()=>{const h=document.querySelector('.custom-table th');h.scrollIntoView({block:'center'});const b=h.querySelector('.column-resizer').getBoundingClientRect();return {x:b.x+b.width/2,y:b.y+b.height/2,width:h.getBoundingClientRect().width};})()",
    );
    await call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: drag.x, y: drag.y });
    await call('Input.dispatchMouseEvent', {
      type: 'mousePressed',
      x: drag.x,
      y: drag.y,
      button: 'left',
      clickCount: 1,
    });
    await call('Input.dispatchMouseEvent', {
      type: 'mouseMoved',
      x: drag.x + 40,
      y: drag.y,
      button: 'left',
      buttons: 1,
    });
    await call('Input.dispatchMouseEvent', {
      type: 'mouseReleased',
      x: drag.x + 40,
      y: drag.y,
      button: 'left',
      clickCount: 1,
    });
    await assert(
      `parseFloat(document.querySelector('.custom-table th').style.width)>=${drag.width + 30}`,
      'Pointer drag resizes column',
    );
    await evaluate("document.querySelector('.custom-table').parentElement.scrollTop=120");
    await assert(
      "Math.abs(document.querySelector('.custom-table th').getBoundingClientRect().top-document.querySelector('.custom-table').parentElement.getBoundingClientRect().top)<3",
      'Table header stays fixed on vertical scroll',
    );
    await capture('workspace-sticky-table.png');
    await evaluate("document.querySelector('.custom-table').parentElement.scrollTop=0;window.scrollTo(0,0)");
    await evaluate(
      "var sel=document.querySelector('select[aria-label=\"每页数量\"]');Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(sel,'50');sel.dispatchEvent(new Event('change',{bubbles:true}));",
    );
    await wait("document.querySelector('select[aria-label=\"每页数量\"]').value==='50'");
    await evaluate(
      "var input=document.querySelector('.keyword-filter input');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'洗');input.dispatchEvent(new Event('input',{bubbles:true}));",
    );
    await pause(700);
    await evaluate(
      "var input=document.querySelector('input[aria-label=\"筛选方案名称\"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'验收筛选');input.dispatchEvent(new Event('input',{bubbles:true}));",
    );
    await pause(100);
    await click('保存当前筛选');
    await evaluate('location.reload()');
    await wait("document.querySelectorAll('.custom-table tbody tr').length>0");
    await assert(
      "document.querySelector('.keyword-filter input').value==='洗'&&document.querySelector('select[aria-label=\"每页数量\"]').value==='50'",
      'Filters and page size survive reload',
    );
    await assert(
      "[...document.querySelectorAll('.custom-table th')].find(e=>e.textContent==='累计销量').hidden&&!!document.querySelector('.custom-table th').style.width",
      'Column settings survive reload',
    );
    await assert(
      "[...document.querySelector('select[aria-label=\"常用筛选\"]').options].some(o=>o.value==='验收筛选')",
      'Saved preset survives reload',
    );
    await wait("[...document.querySelectorAll('.thumbnail-button img')].some(i=>i.complete&&i.naturalWidth>0)");
    await evaluate(
      "[...document.querySelectorAll('.thumbnail-button img')].find(i=>i.complete&&i.naturalWidth>0).parentElement.click()",
    );
    await wait("!!document.querySelector('.image-preview img')");
    await assert("document.querySelector('.image-preview img').naturalWidth>0", 'Image preview loads');
    await capture('workspace-image.png');
    await click('关闭大图');
    await click('商品综合详情');
    await wait("document.querySelectorAll('.product-sections>section').length===6");
    await assert(
      "document.querySelector('.product-overview').textContent.includes('库存')",
      'Integrated product details load',
    );
    await capture('workspace-product.png');
    await evaluate("document.querySelector('.product-overview .dialog-head button').click()");
    console.log(
      'Export',
      await evaluate(
        "(async()=>{const r=await fetch('/api/data/products?keyword='+encodeURIComponent('洗')+'&export=true');const a=new Uint8Array(await r.arrayBuffer());return {status:r.status,type:r.headers.get('content-type'),disposition:!!r.headers.get('content-disposition'),zip:a[0]===80&&a[1]===75,bytes:a.length};})()",
      ),
    );
    await capture('workspace-products.png');
    await click('订单售后');
    await wait("document.querySelector('.business-heading h2')?.textContent==='重点待办中心'");
    await wait("!!document.querySelector('.column-settings')", 90);
    await assert("!!document.querySelector('.column-settings')", 'Unified todo center available');
    await capture('workspace-todos.png');
    await call('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
    await pause(500);
    await assert('document.documentElement.scrollWidth<=innerWidth+1', 'Mobile page stays within viewport');
    await capture('workspace-mobile.png');
    await call('Emulation.clearDeviceMetricsOverride');
    // Tax review writes use a browser-local fixture. Never confirm live financial records.
    await evaluate(`(()=>{
      const realFetch=window.fetch.bind(window);const revision='a'.repeat(64),amounts={trade_income:2,coupon_subsidy:0,refund:0,coupon_refund:0,tax_amount:2};
      const shop={...amounts,id:'2026-09',period:'2026-09',shop_id:999,shop_name:'验收测试店铺',db_comparison:{changed:true,baseline_at:1,checked_at:2,differences:[{field:'trade_income_cents',label:'交易收入',money:true,before:100,after:200}],review:{confirmed:false,revision}}};
      window.__taxFixture={review:{revision,confirmed:false,note:'',reviewed_at:null},latest:amounts,history_total:2,history:[{id:2,created:2,payload:amounts},{id:1,created:1,payload:{...amounts,trade_income:1,tax_amount:1}}],reviews:[]};
      window.fetch=async(input,options={})=>{const url=String(input);if(url.startsWith('/api/tax-review/999/')){if(options.method==='POST'){const body=JSON.parse(options.body);window.__taxFixture.review={...body,reviewed_at:3};window.__taxFixture.reviews.unshift({...body,created:3});}return Response.json(window.__taxFixture);}if(url.startsWith('/api/data/tax?'))return Response.json({sources:[],subjects:[],tax_summary:amounts,rows:[{...amounts,id:'test',shop_name:'验收测试店铺',period:'2026-09',summary_through:'2026-09-30',entity:{company_name:'验收主体',credit_code:'TEST'},db_changed:true,db_reviewed:window.__taxFixture.review.confirmed,shop_breakdown:[shop]}],total:1,page_count:1,difference_count:1});return realFetch(input,options);};
    })()`);
    await click('资金报税');
    await click('报税信息');
    await wait("document.querySelector('.tax-difference-row')!==null");
    await click('详情');
    await wait("!!document.querySelector('.tax-review textarea')");
    await evaluate(
      "var input=document.querySelector('.tax-review textarea');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(input,'验收核对备注');input.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('.review-checkbox input').click();",
    );
    await pause(100);
    await click('保存核对记录');
    await wait("document.querySelector('.tax-review').textContent.includes('上次核对：')");
    await assert(
      "window.__taxFixture.review.confirmed&&window.__taxFixture.review.note==='验收核对备注'",
      'Tax review saves current revision and note with fixture',
    );
    await assert("document.querySelector('.tax-review').textContent.includes('2 个版本')", 'Tax history visible');
    await capture('workspace-tax-review.png');
    await evaluate("document.querySelector('.record-dialog .dialog-head button').click()");
    await wait("document.querySelector('.difference-badge').textContent.includes('已确认')");
    if (errors.length) throw Error('Browser errors: ' + errors.join('\n'));
    console.log('Workspace UI checks passed');
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
