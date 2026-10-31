const { test, expect } = require('@playwright/test');
const base = process.env.ECMP_UI_TEST_URL || 'http://127.0.0.1:3212';
test.use({ launchOptions: { channel: 'chrome' }, viewport: { width: 1440, height: 900 } });
test('patched production build preserves authentication and main pages', async ({ page, context }) => {
  test.setTimeout(120000);
  test.skip(!process.env.ECMP_UI_TEST_PASSWORD, 'Center authentication required');
  expect((await context.request.get(base + '/api/shops')).status()).toBe(401);
  expect(
    (await context.request.post(base + '/api/auth/login', { data: { username: process.env.ECMP_UI_TEST_USERNAME || 'testadmin', password: process.env.ECMP_UI_TEST_PASSWORD } })).ok(),
  ).toBeTruthy();
  const response = await context.request.get(base + '/api/shops');
  expect(response.ok()).toBeTruthy();
  expect(Array.isArray(await response.json())).toBeTruthy();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(base + '/?view=overview');
  await expect(page.locator('.overview-dashboard')).toBeVisible({ timeout: 45000 });
  await expect(page.locator('.ov-kpi.ant-card')).toHaveCount(6);
  await page.goto(base + '/?view=browser_workspace');
  await expect(page.getByRole('heading', { name: '店铺浏览器工作台' })).toBeVisible();
  await page.goto(base + '/?view=report_exports');
  await expect(page.getByRole('heading', { name: '报表导出', exact: true }).first()).toBeVisible({ timeout: 30000 });
  expect(errors).toEqual([]);
});
