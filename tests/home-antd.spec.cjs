const { test, expect } = require('@playwright/test');
const base = process.env.ECMP_UI_TEST_URL || 'http://127.0.0.1:3212';
test.use({ actionTimeout: 15000, viewport: { width: 1440, height: 960 }, launchOptions: { channel: 'chrome' } });
test('home Ant controls preserve chart, warnings, table and narrow layout', async ({ page, context }) => {
  test.setTimeout(180000);
  test.skip(!process.env.ECMP_UI_TEST_PASSWORD, 'Local center token required');
  expect(
    (await context.request.post(base + '/api/auth/login', { data: { username: process.env.ECMP_UI_TEST_USERNAME || 'testadmin', password: process.env.ECMP_UI_TEST_PASSWORD } })).ok(),
  ).toBeTruthy();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(base + '/?view=overview');
  await expect(page.locator('.overview-dashboard')).toBeVisible({ timeout: 45000 });
  await expect(page.locator('.ov-kpi.ant-card')).toHaveCount(6);
  await expect(page.locator('.ov-kpi .ant-card-head')).toHaveCount(0);
  await expect(page.locator('.ov-revenue .ant-statistic')).toBeVisible();
  await expect(page.locator('.ov-controls .ant-select').first()).toBeVisible();
  await page.locator('.ov-shop-select .ant-select').click();
  await expect(page.locator('.ant-select-dropdown:visible .ant-select-item-option').first()).toBeVisible();
  await page.locator('.ant-select-dropdown:visible .ant-select-item-option').first().click();
  await expect(page.locator('.ant-select-dropdown:visible .ant-select-item-option-selected')).toHaveCount(1);
  await page.keyboard.press('Escape');
  await page.locator('.ov-shop-select .ant-select-clear').click();
  await expect(page.locator('.ov-shop-select')).toContainText('全部店铺');
  await expect(page.getByRole('button', { name: '暂停首页采集', exact: true })).toHaveClass(/ant-btn/);
  await page.locator('.ov-trend .ant-segmented').getByText('订单数', { exact: true }).click();
  await expect(page.locator('.ov-trend .ant-segmented-item-selected')).toContainText('订单数');
  await expect(page.locator('.ov-store-section .ant-table')).toBeVisible();
  await page.locator('.ov-store-section').scrollIntoViewIfNeeded();
  await expect(page.locator('.ov-store-section .ant-table-row').first()).toBeVisible({ timeout: 30000 });
  await page.locator('.ov-store-section .ant-table-thead th').filter({ hasText: '成交金额' }).first().click();
  await page.locator('.ov-heading').scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'tmp/home-antd-desktop.png' });
  await page.evaluate(() => (document.documentElement.dataset.theme = 'dark'));
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect
    .poll(() => page.locator('.home-collection-control').evaluate((e) => getComputedStyle(e).backgroundColor))
    .not.toBe('rgb(255, 255, 255)');
  await expect
    .poll(() =>
      page
        .locator('.ov-controls .ant-select')
        .first()
        .evaluate((e) => getComputedStyle(e).backgroundColor),
    )
    .not.toBe('rgb(255, 255, 255)');
  await page.screenshot({ path: 'tmp/home-antd-dark.png' });
  await page.evaluate(() => (document.documentElement.dataset.theme = 'white'));
  for (const width of [1024, 768, 480]) {
    await page.setViewportSize({ width, height: 800 });
    await page.locator('.ov-heading').scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
    await page.screenshot({ path: `tmp/home-antd-${width}.png` });
    const scroller = page.locator('.ov-store-section .ant-table-content');
    expect(await scroller.evaluate((e) => e.scrollWidth > e.clientWidth)).toBeTruthy();
  }
  await page.setViewportSize({ width: 1440, height: 960 });
  await page
    .locator('.ov-urgent-grid')
    .getByRole('button', { name: /即将逾期发货/ })
    .click();
  await expect(page.getByRole('heading', { name: '订单查询', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '即将逾期发货', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await page.goto(base + '/?view=overview');
  await page
    .locator('.ov-urgent-grid')
    .getByRole('button', { name: /售后过期预警/ })
    .click();
  await expect(page.getByRole('heading', { name: '售后工作台', exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});
