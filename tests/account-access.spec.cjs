const { test, expect } = require('@playwright/test');
const base = process.env.ECMP_UI_TEST_URL || 'http://127.0.0.1:3212';
const username = 'testadmin',
  password = 'test-only-admin-password';
test.use({ launchOptions: { channel: 'chrome' }, viewport: { width: 1440, height: 1000 } });
test.describe.configure({ mode: 'serial' });
test.beforeAll(async ({ request }) => {
  const target = new URL(base);
  test.skip(process.env.ECMP_UI_TEST_ISOLATED !== '1' || target.hostname !== '127.0.0.1' || target.port === '3211', 'Requires an explicitly isolated local test server; never initialize a production administrator');
  const session = await (await request.get(base + '/api/auth/session')).json();
  if (!session.initialized)
    expect((await request.post(base + '/api/auth/setup', { data: { username, password } })).ok()).toBeTruthy();
});
test('anonymous preview is removed and account setup cannot be repeated', async ({ request, page }) => {
  expect((await request.get(base + '/api/data/transactions')).status()).toBe(401);
  expect((await request.get(base + '/api/home/snapshot')).status()).toBe(401);
  expect((await request.get(base + '/api/public/overview')).status()).not.toBe(200);
  expect((await request.post(base + '/api/auth/setup', { data: { username: 'intruder', password } })).status()).toBe(
    403,
  );
  await page.goto(base + '/center');
  await expect(page.getByText('登录多店铺数据工作台', { exact: true })).toBeVisible();
  await expect(page.locator('.public-overview')).toHaveCount(0);
  await page.screenshot({ path: 'tmp/account-login.png', fullPage: true });
});
test('page-level data and write permissions are enforced and revoked immediately', async ({ request, browser }) => {
  expect((await request.post(base + '/api/auth/login', { data: { username, password } })).ok()).toBeTruthy();
  const readerName = 'readonly-' + Date.now();
  const result = await request.post(base + '/api/auth/users', {
    data: { username: readerName, password, role: 'user', permissions: { transactions: 'read' } },
  });
  expect(result.ok()).toBeTruthy();
  const account = await result.json();
  const context = await browser.newContext();
  expect(
    (await context.request.post(base + '/api/auth/login', { data: { username: readerName, password } })).ok(),
  ).toBeTruthy();
  expect(new URL((await context.request.get(base + '/?view=shops')).url()).searchParams.get('view')).toBe(
    'transactions',
  );
  for (const route of ['data/orders', 'profit', 'system/settings', 'center/nodes', 'auth/users'])
    expect((await context.request.get(base + '/api/' + route)).status(), route).toBe(403);
  for (const route of ['data/transactions/sync', 'nodes/2/proxy/data/transactions/sync', 'shops/2/show'])
    expect((await context.request.post(base + '/api/' + route, { data: {} })).status(), route).toBe(403);
  const saved = await request.put(base + '/api/auth/users/' + account.id, { data: { ...account, disabled: true } });
  expect(saved.ok()).toBeTruthy();
  expect((await context.request.get(base + '/api/data/transactions')).status()).toBe(401);
  await context.close();
});
test('transaction row geometry and values stay stable on hover and keyboard focus', async ({ page, context }) => {
  test.setTimeout(90000);
  expect((await context.request.post(base + '/api/auth/login', { data: { username, password } })).ok()).toBeTruthy();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('console', (m) => {
    if (m.type() === 'error') console.log('UI console:', m.text());
  });
  const shops = [1, 2, 3].map((id) => ({
    id,
    shop_id: id,
    name: '交易测试店' + id,
    shop_name: '交易测试店' + id,
    platform: 'pdd',
    state: 'sleeping',
    operator: '测试运营',
    operators: ['测试运营'],
  }));
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/connection' || url.pathname.startsWith('/api/auth/')) return route.continue();
    if (url.pathname === '/api/shops') return route.fulfill({ json: shops });
    if (url.pathname === '/api/system/summary') return route.fulfill({ json: { summary: { running: 0, queued: 0 } } });
    if (url.pathname === '/api/active-node') return route.fulfill({ json: { id: 0 } });
    if (url.pathname === '/api/events') return route.fulfill({ contentType: 'text/event-stream', body: ': test\n\n' });
    if (url.pathname === '/api/data/transactions')
      return route.fulfill({
        json: {
          scope: { trade_range: 'realtime' },
          rows: [],
          sources: [],
          total: 0,
          page_count: 1,
          trade_totals: { sales: 1234.56, orders: 8, buyers: 7 },
          metrics: shops.map((s) => ({
            ...s,
            summary: { sales: s.id * 1234.56, orders: s.id * 100, buyers: s.id * 77, conversion: 33.3, average: 12.35 },
            trend: [],
            regions: [],
            annual: [],
          })),
        },
      });
    return route.fulfill({ json: url.pathname === '/api/operators' || url.pathname === '/api/nodes' ? [] : {} });
  });
  const main = await page.goto(base + '/?view=transactions');
  console.log('transaction page', main.status(), page.url());
  const row = page.locator('.transaction-data .ant-table-tbody tr[data-shop-id="2"]');
  try {
    await expect(row).toBeVisible({ timeout: 15000 });
  } catch (e) {
    console.log('UI errors', errors);
    throw e;
  }
  const measure = () =>
    row.evaluate((tr) =>
      [...tr.cells].map((td) => {
        const b = td.getBoundingClientRect(),
          s = getComputedStyle(td);
        return {
          text: td.innerText,
          x: b.x,
          y: b.y,
          width: b.width,
          height: b.height,
          font: s.fontSize,
          padding: s.padding,
          display: s.display,
          position: s.position,
        };
      }),
    );
  await page.mouse.move(10, 10);
  const original = await measure();
  for (let i = 0; i < 3; i++) {
    await row.hover();
    await page.waitForTimeout(180);
    expect(await measure()).toEqual(original);
    await row.getByRole('button').focus();
    await page.waitForTimeout(180);
    expect(await measure()).toEqual(original);
    await page.mouse.move(10, 10);
  }
  await page.screenshot({ path: 'tmp/transaction-row-stable.png', fullPage: true });
  expect(errors).toEqual([]);
});

test('account manager exposes page permissions in a usable narrow-screen dialog', async ({ page, context }) => {
  expect((await context.request.post(base + '/api/auth/login', { data: { username, password } })).ok()).toBeTruthy();
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/connection' || path.startsWith('/api/auth/')) return route.continue();
    if (path === '/api/events') return route.fulfill({ contentType: 'text/event-stream', body: ': test\n\n' });
    return route.fulfill({
      json: ['shops', 'operators', 'nodes'].some((p) => path === '/api/' + p)
        ? []
        : path === '/api/system/summary'
          ? { summary: { running: 0, queued: 0 } }
          : {},
    });
  });
  await page.goto(base + '/?view=accounts');
  await expect(page.getByRole('heading', { name: '账号与权限' })).toBeVisible();
  await page.getByRole('button', { name: '新增账号' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('交易数据', { exact: true })).toBeVisible();
  await expect(dialog.locator('.account-permissions')).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(async () => {
      const box = await dialog.boundingBox();
      return box.x >= 0 && box.x + box.width <= 390;
    })
    .toBe(true);
  await page.screenshot({ path: 'tmp/account-permissions-mobile.png', fullPage: true });
  await expect(dialog.locator('.ant-modal-footer button').last()).toBeInViewport();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
