const { test, expect } = require('@playwright/test');
test.use({ launchOptions: { channel: 'chrome' }, viewport: { width: 1440, height: 900 } });
test('browser workspace filters and scoped controls', async ({ page, context }) => {
  const base = process.env.ECMP_UI_TEST_URL || 'http://127.0.0.1:3211';
  test.skip(!process.env.ECMP_UI_TEST_PASSWORD, 'A test account is required');
  expect((await context.request.post(base + '/api/auth/login', { data: { username: process.env.ECMP_UI_TEST_USERNAME || 'testadmin', password: process.env.ECMP_UI_TEST_PASSWORD } })).ok()).toBeTruthy();
  const shops = [
    {
      id: 2,
      name: '独立店铺甲',
      platform: 'pdd',
      state: 'stopped',
      browser: 'system',
      node_id: 'a',
      machine_name: '本机',
    },
    {
      id: 21,
      name: '独立店铺乙',
      platform: 'pdd',
      state: 'logged_in',
      browser: 'system',
      node_id: 'b',
      machine_name: '远程节点',
      visible: true,
    },
  ];
  const actions = [],
    errors = [];
  page.on('pageerror', (e) => {
    errors.push(e.message);
    console.log(e.stack);
  });
  await page.route('**/api/**', (route) => {
    const p = new URL(route.request().url()).pathname;
    if (p.match(/\/shops\/\d+\//)) {
      actions.push(p);
      return route.fulfill({ json: { ok: true } });
    }
    return route.fulfill({
      json:
        p === '/api/shops'
          ? shops
          : p === '/api/connection'
            ? { center: true, connected: true, cacheScope: 'browser-test', user: { id: 'test', role: 'admin', permissions: {} } }
            : p === '/api/nodes'
              ? []
              : p === '/api/system/summary'
                ? { summary: { running: 0, queued: 0 } }
                : {},
    });
  });
  await page.goto(base + '/?view=browser_workspace');
  await expect(page.getByRole('heading', { name: '店铺浏览器工作台' })).toBeVisible();
  await expect(page.getByText('独立店铺甲', { exact: false }).first()).toBeVisible();
  expect(actions).toEqual([]);
  await page.getByLabel('搜索店铺浏览器').fill('店铺甲');
  await expect(page.getByText('独立店铺乙', { exact: false })).toHaveCount(0);
  await page.getByRole('button', { name: '启动 / 登录', exact: true }).click();
  await expect.poll(() => actions.includes('/api/shops/2/start')).toBeTruthy();
  await page.getByLabel('搜索店铺浏览器').fill('');
  await page.getByRole('button', { name: '隐藏窗口', exact: true }).click();
  await expect.poll(() => actions.includes('/api/shops/21/hide')).toBeTruthy();
  await page.setViewportSize({ width: 480, height: 800 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  expect(errors).toEqual([]);
});
