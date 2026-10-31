// All shop/API/remote-frame data is synthetic in an isolated Chrome profile.
const { spawn } = require('node:child_process');
const fs = require('node:fs'),
  path = require('node:path');
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const root = path.resolve(__dirname, '..'),
    origin = process.env.REMOTE_TEST_ORIGIN || 'http://127.0.0.1:3212';
  const chrome = spawn(
    path.join(root, '.browsers/chrome-win64/chrome.exe'),
    [
      '--headless=new',
      '--no-first-run',
      '--window-size=1560,1100',
      '--remote-debugging-port=19377',
      `--user-data-dir=${path.join(root, '.data/ui-remote-viewer')}`,
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  );
  let socket, call;
  try {
    let target;
    for (let i = 0; i < 50; i++) {
      try {
        target = (await (await fetch('http://127.0.0.1:19377/json/list')).json()).find((t) => t.type === 'page');
        if (target) break;
      } catch {}
      await pause(200);
    }
    socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((r, j) => {
      socket.onopen = r;
      socket.onerror = j;
    });
    let seq = 0;
    const pending = new Map(),
      errors = [];
    let screenshots = 0;
    socket.onmessage = (e) => {
      const m = JSON.parse(e.data),
        p = pending.get(m.id);
      if (p) {
        pending.delete(m.id);
        m.error ? p.reject(Error(m.error.message)) : p.resolve(m.result);
      }
      if (m.method === 'Runtime.exceptionThrown')
        errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    };
    call = (method, params = {}) =>
      new Promise((resolve, reject) => {
        const id = ++seq;
        pending.set(id, { resolve, reject });
        socket.send(JSON.stringify({ id, method, params }));
      });
    const evaluate = async (expression) => {
      const r = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
      return r.result.value;
    };
    const wait = async (expression) => {
      for (let i = 0; i < 100; i++) {
        if (await evaluate(expression)) return;
        await pause(150);
      }
      throw Error('Timed out ' + expression + '\n' + (await evaluate('document.body.innerText.slice(-1500)')));
    };
    const assert = async (expression, label) => {
      if (!(await evaluate(expression))) throw Error(label);
      console.log('PASS', label);
    };
    const shop = {
      id: 777,
      name: '远程控制验证店铺',
      username: 'synthetic',
      platform: 'pdd',
      browser: 'system',
      state: 'logged_in',
      visible: false,
      adapter_available: true,
    };
    socket.addEventListener('message', async (e) => {
      const m = JSON.parse(e.data);
      if (m.method !== 'Fetch.requestPaused') return;
      const p = m.params,
        u = new URL(p.request.url);
      try {
        let data =
          u.pathname === '/api/connection'
            ? { connected: true, url: 'http://127.0.0.1:8000', cacheScope: 'remote-ui-test' }
            : u.pathname === '/api/shops'
              ? [shop]
              : u.pathname === '/api/browsers'
                ? [{ id: 'system', available: true }]
                : u.pathname === '/api/operators' || u.pathname === '/api/nodes'
                  ? []
                  : u.pathname === '/api/active-node'
                    ? { id: 0 }
                    : u.pathname === '/api/system/summary'
                      ? {
                          summary: { running: 0, queued: 0, errors: 0 },
                          collection: { enabled: true, realtime_seconds: 60 },
                        }
                      : {};
        let type = 'application/json',
          body = Buffer.from(JSON.stringify(data)).toString('base64');
        if (u.pathname.endsWith('/screenshot')) {
          screenshots++;
          type = 'image/jpeg';
          body = await evaluate('window.__jpeg');
        }
        await call('Fetch.fulfillRequest', {
          requestId: p.requestId,
          responseCode: 200,
          responseHeaders: [{ name: 'Content-Type', value: type }],
          body,
        });
      } catch (e) {
        if (!e.message.includes('Invalid InterceptionId')) errors.push(e.message);
      }
    });
    await call('Runtime.enable');
    await call('Page.enable');
    await call('Fetch.enable', { patterns: [{ urlPattern: '*/api/*' }] });
    await call('Page.addScriptToEvaluateOnNewDocument', {
      source: `
   if(location.origin===${JSON.stringify(origin)}){
    localStorage.clear();sessionStorage.clear();localStorage.setItem('pdd-manager:view','shops');
    const c=document.createElement('canvas');c.width=1200;c.height=650;const x=c.getContext('2d');x.fillStyle='#153655';x.fillRect(0,0,1200,650);x.fillStyle='#ffffff';x.font='32px sans-serif';x.fillText('Remote browser · test frame',60,85);x.fillStyle='#2187db';x.fillRect(60,130,1080,420);window.__jpeg=c.toDataURL('image/jpeg').split(',')[1];
    window.__sockets=[];window.__sent=[];window.__fail=false;
    window.EventSource=class{static OPEN=1;constructor(){this.readyState=1;}addEventListener(){}close(){}};
    window.WebSocket=class{static OPEN=1;constructor(url){this.url=url;this.readyState=0;this.id=0;window.__sockets.push(this);setTimeout(()=>{if(window.__fail){this.close(4406);return;}this.readyState=1;this.onopen?.();this.emit({type:'transport',dedicated:true});this.emit({type:'mode',mode:'stream'});this.frame();},30);}
     emit(value){if(this.readyState===1)this.onmessage?.({data:JSON.stringify(value)});}
     frame(){this.emit({type:'frame',id:++this.id,data:window.__jpeg,width:1200,height:650});}
     send(raw){const m=JSON.parse(raw);window.__sent.push(m);if(m.type==='ping')this.emit({type:'pong',at:m.at});if(m.type==='input')setTimeout(()=>this.emit({type:'input_ack',id:m.id}),150);if(m.type==='ack')this.timer=setTimeout(()=>this.frame(),120);}
     close(code=1000){clearTimeout(this.timer);this.readyState=3;this.onclose?.({code});}
    };
   }`,
    });
    await call('Page.navigate', { url: origin });
    await wait("!!document.querySelector('.shop-card .primary')");
    await evaluate("document.querySelector('.shop-card .primary').click()");
    await wait("document.querySelector('.remote-screen canvas')?.width===1200");
    await assert(
      "document.querySelector('.remote-toolbar').innerText.includes('持续推流')",
      'continuous stream and decoded canvas',
    );
    await evaluate(
      "const c=document.querySelector('.remote-screen canvas');c.setPointerCapture=()=>{};const r=c.getBoundingClientRect();for(let i=0;i<80;i++)c.dispatchEvent(new PointerEvent(i===0?'pointerdown':i===79?'pointerup':'pointermove',{bubbles:true,pointerId:1,clientX:r.left+20+i,clientY:r.top+50}));",
    );
    await pause(800);
    await assert(
      "(()=>{const e=window.__sent.filter(m=>m.type==='input').flatMap(m=>m.events);return e.length<10&&e[0].type==='mousePressed'&&e.at(-1).type==='mouseReleased';})()",
      'drag events coalesced without losing press/release',
    );
    await evaluate(
      "(async()=>{window.__sent=[];const c=document.querySelector('.remote-screen canvas'),r=c.getBoundingClientRect();c.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:1,clientX:r.left+20,clientY:r.top+50}));for(let i=0;i<40;i++){c.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:1,clientX:r.left+20+i,clientY:r.top+50}));await new Promise(resolve=>setTimeout(resolve,10));}window.__movedDuringDrag=window.__sent.some(m=>m.type==='input'&&m.events.some(e=>e.type==='mouseMoved'));c.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:1,clientX:r.left+60,clientY:r.top+50}));})()",
    );
    await assert('window.__movedDuringDrag', 'continuous drag sends updates before the pointer stops');
    await pause(600);
    await evaluate(
      "const select=document.querySelector('[aria-label=远程画质]');select.value='clear';select.dispatchEvent(new Event('change',{bubbles:true}));",
    );
    await assert(
      "window.__sent.some(m=>m.type==='quality'&&m.preset==='clear')",
      'quality change sent on persistent channel',
    );
    await assert("window.__sent.some(m=>m.type==='quality'&&m.frame_window===2)", 'bounded frame pipeline negotiated');
    await assert(
      "document.querySelector('.remote-toolbar').innerText.includes('独立远控通道')",
      'dedicated channel displayed',
    );
    await pause(1200);
    await assert(
      "!document.querySelector('.remote-toolbar').innerText.includes('往返 —')",
      'measured round-trip shown',
    );
    await evaluate('window.__sockets.at(-1).close(1012)');
    await wait('window.__sockets.length>=2&&window.__sockets.at(-1).readyState===1');
    console.log('PASS automatic reconnect');
    await call('Page.captureScreenshot', { format: 'png' }).then((r) =>
      fs.writeFileSync(path.join(root, '.data/remote-viewer-ui.png'), Buffer.from(r.data, 'base64')),
    );
    await evaluate('window.__fail=true;window.__sockets.at(-1).close(1012)');
    await wait("document.querySelector('.remote-toolbar').innerText.includes('兼容截图模式')");
    await pause(900);
    if (!screenshots) throw Error('No fallback screenshot');
    console.log('PASS legacy screenshot fallback');
    await evaluate("document.querySelector('.remote-viewer .dialog-head button').click()");
    await pause(900);
    const after = screenshots;
    await pause(1400);
    if (screenshots !== after) throw Error('Polling after close');
    await assert(
      "!document.querySelector('.remote-viewer')&&window.__sockets.every(s=>s.readyState===3)",
      'close stops sockets and fallback polling',
    );
    if (errors.length) throw Error(errors.join('\n'));
    console.log('ALL REMOTE VIEWER UI CHECKS PASSED');
  } finally {
    if (call)
      try {
        await call('Browser.close');
      } catch {}
    socket?.close();
    chrome.kill();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
