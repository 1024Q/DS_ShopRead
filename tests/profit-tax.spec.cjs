const { test, expect } = require('@playwright/test');
const base = process.env.ECMP_UI_TEST_URL || 'http://127.0.0.1:3212';
test.use({ launchOptions: { channel: 'chrome' }, viewport: { width: 1440, height: 960 } });
test('tax settings, protected history confirmation and responsive report columns', async ({ page, context }) => {
  test.setTimeout(120000);
  test.skip(!process.env.ECMP_UI_TEST_PASSWORD, 'Local authentication required');
  expect(
    (await context.request.post(base + '/api/auth/login', { data: { username: process.env.ECMP_UI_TEST_USERNAME || 'testadmin', password: process.env.ECMP_UI_TEST_PASSWORD } })).ok(),
  ).toBeTruthy();
  const response = await context.request.get(base + '/api/profit-tax?month=2026-09');
  expect(response.ok()).toBeTruthy();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(base + '/?view=profit_tax');
  await expect(page.getByRole('heading', { name: '税费预估与核对', exact: true })).toBeVisible();
  await expect(page.getByLabel('税点', { exact: true })).toBeVisible();
  await page.getByLabel('税点', { exact: true }).fill('3');
  await page.getByRole('checkbox', { name: '同时更新所选月份已有报表并重新核算' }).check();
  let sent = null;
  await page.route('**/api/profit-tax/0/*', async (route) => {
    sent = route.request().postDataJSON();
    await route.fulfill({ json: { id: 'test-only', revision: 1, affected: 0 } });
  });
  await page.getByRole('button', { name: '保存税费版本' }).click();
  await expect(page.locator('.ant-modal-confirm')).toBeVisible();
  await page.getByRole('button', { name: '确认重新核算', exact: true }).click();
  await expect.poll(() => sent?.apply_existing).toBe(true);
  expect(Number(sent.config.rate)).toBe(3);
  await expect(page.getByText(/税费版本已保存/)).toBeVisible();
  await expect(page.locator('.ant-modal-confirm')).toBeHidden();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: 'tmp/tax-settings.png' });
  await page.setViewportSize({ width: 480, height: 800 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  await page.goto(base + '/?view=profit');
  await expect(page.getByRole('heading', { name: '利润核算', exact: true })).toBeVisible();
  await expect(page.getByText('税后预估利润', { exact: true }).first()).toBeAttached();
  const detail = await context.request.post(base + '/api/table-tools/orders', {
    data: { month: '2026-09', page_size: 10 },
  });
  expect(detail.ok()).toBeTruthy();
  expect((await detail.json()).columns.map((c) => c.id)).toContain('tax_deduction');
  expect(errors).toEqual([]);
});
