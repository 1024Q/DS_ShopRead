const fs = require('node:fs');
(async () => {
  const targets = await (await fetch('http://127.0.0.1:19210/json/list')).json();
  const target = targets.find((t) => t.type === 'page' && t.url.startsWith('http://127.0.0.1:3210'));
  if (!target) throw new Error('管理窗口未打开');
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', reject, { once: true });
  });
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    const task = pending.get(message.id);
    if (task) {
      pending.delete(message.id);
      message.error ? task.reject(new Error(message.error.message)) : task.resolve(message.result);
    }
  });
  const call = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const number = ++id;
      pending.set(number, { resolve, reject });
      ws.send(JSON.stringify({ id: number, method, params }));
    });
  const evaluate = async (expression) => {
    const result = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
    return result.result.value;
  };
  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const waitFor = async (expression) => {
    for (let n = 0; n < 50; n++) {
      if (await evaluate(expression)) return;
      await pause(200);
    }
    throw new Error('页面操作等待超时: ' + expression);
  };
  const click = async (text, root = 'document') => {
    await evaluate(
      `(() => { const button=[...(${root}).querySelectorAll('button')].find(e=>e.textContent.trim()===${JSON.stringify(text)}); if(!button)throw Error('按钮不存在'); button.click(); })()`,
    );
    await pause(150);
  };
  const fill = async (index, value) => {
    await evaluate(
      `(() => {const input=document.querySelectorAll('form[role=dialog] input')[${index}]; Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(value)}); input.dispatchEvent(new Event('input',{bubbles:true})); input.dispatchEvent(new Event('change',{bubbles:true}));})()`,
    );
    await pause(150);
  };
  const name = '功能验证临时店铺';
  const edited = '功能验证已修改';
  const username = 'crud-test-' + Date.now();
  let created;
  try {
    await call('Page.navigate', { url: 'http://127.0.0.1:3210/' });
    await waitFor("[...document.querySelectorAll('button')].some(e=>e.textContent==='编辑店铺')");
    await click('＋ 添加店铺');
    await fill(0, name);
    await fill(1, username);
    await fill(2, 'Temporary-test-password');
    await click('添加店铺', "document.querySelector('form[role=dialog]')");
    await waitFor(
      `document.body.innerText.includes(${JSON.stringify(name)}) && !document.querySelector('form[role=dialog]')`,
    );
    created = true;
    const card = (text) =>
      `[...document.querySelectorAll('article')].find(e=>e.querySelector('h3')?.textContent===${JSON.stringify(text)})`;
    await click('编辑店铺', card(name));
    if (await evaluate("document.querySelector('input[type=password]').value !== ''"))
      throw new Error('编辑窗口不应回显密码');
    await fill(0, edited);
    await click('保存修改', "document.querySelector('form[role=dialog]')");
    await waitFor(
      `document.body.innerText.includes(${JSON.stringify(edited)}) && !document.querySelector('form[role=dialog]')`,
    );
    await click('删除店铺', card(edited));
    await click('取消', "document.querySelector('section[role=dialog]')");
    if (!(await evaluate(`!!(${card(edited)})`))) throw new Error('取消删除错误');
    await click('删除店铺', card(edited));
    await click('确认删除', "document.querySelector('section[role=dialog]')");
    await waitFor(`!document.body.innerText.includes(${JSON.stringify(edited)})`);
    const screenshot = await call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync('dashboard-preview.png', Buffer.from(screenshot.data, 'base64'));
    console.log('UI passed: add, edit, blank password, cancel delete, confirm delete. Temporary shop removed.');
  } finally {
    // Remove only this run's temporary account if a UI assertion failed.
    await evaluate(
      `(async()=>{const rows=await (await fetch('/api/shops')).json();const row=rows.find(r=>r.username===${JSON.stringify(username)});if(row)await fetch('/api/shops/'+row.id,{method:'DELETE'});})()`,
    ).catch(() => {});
    ws.close();
  }
})().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
