const { test, expect } = require('@playwright/test');
const base = process.env.ECMP_UI_TEST_URL || 'http://127.0.0.1:3212';
test.use({ actionTimeout: 15000, viewport: { width: 1440, height: 960 }, launchOptions: { channel: 'chrome' } });
test('business UI: tables, menus, filters, settings and home isolation', async ({ page, context }) => {
  test.setTimeout(180000);
  test.skip(!process.env.ECMP_UI_TEST_PASSWORD, 'Local center token is required for this read-only smoke test');
  const login = await context.request.post(base + '/api/auth/login', {
    data: { username: process.env.ECMP_UI_TEST_USERNAME || 'testadmin', password: process.env.ECMP_UI_TEST_PASSWORD },
  });
  expect(login.ok()).toBeTruthy();
  // Exercise the form even when all collectors are deliberately stopped.
  // This fixture is browser-local and never writes node settings.
  await page.route('**/api/system/settings', async (route) => {
    expect(route.request().method()).toBe('GET');
    await route.fulfill({
      json: {
        collection: {
          enabled: true,
          realtime_seconds: 60,
          custom_seconds: 300,
          custom2_seconds: 600,
          custom3_seconds: 1800,
          daily_time: '03:00',
          frequency_names: {},
          frequency_timeouts: {},
          frequency_groups: [],
        },
        node_configs: [],
      },
    });
  });
  const failures = [];
  page.on('pageerror', (e) => failures.push(e.message));
  await page.goto(base + '/?view=profit');
  await expect(page.locator('.admin-table .ant-table-row').first()).toBeVisible({ timeout: 45000 });
  await expect(page.locator('.filters .ant-select-content').first()).toContainText('全部店铺');
  await page.getByRole('button', { name: '列设置', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '列设置', exact: true })).toBeVisible();
  await page
    .getByRole('dialog', { name: '列设置', exact: true })
    .getByRole('button', { name: '关闭', exact: true })
    .click();
  await page.locator('.admin-table .ant-table-row input[type=checkbox]').first().check();
  await expect(page.locator('.admin-selection')).toContainText('已选');
  await page.locator('.admin-table-toolbar .ant-dropdown-trigger').click();
  await expect(page.getByText('选中记录 · Excel', { exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.getByRole('heading', { name: '利润核算', exact: true }).click();
  await page.getByRole('button', { name: '取消选择', exact: true }).click();
  await expect(page.locator('.admin-selection')).toBeHidden();
  await page.getByRole('heading', { name: '利润核算', exact: true }).click();
  await expect(page.getByText('选中记录 · Excel', { exact: true })).toBeHidden();
  await page.locator('.admin-table .ant-table-header').getByText('实收金额', { exact: true }).click();
  const amounts = await page
    .locator('.admin-table .ant-table-row')
    .evaluateAll((rows) => rows.map((r) => Number(r.querySelectorAll('td')[4].textContent.replaceAll(',', ''))));
  expect(amounts).toEqual([...amounts].sort((a, b) => a - b));
  await page.screenshot({ path: 'tmp/admin-verified-profit.png' });
  await page.goto(base + '/?view=report_exports');
  await expect(page.locator('.admin-picker-list .ant-checkbox-wrapper').first()).toBeVisible({ timeout: 30000 });
  await page.getByLabel('搜索导出店铺', { exact: true }).fill('不存在的店铺');
  await expect(page.getByText('没有匹配的店铺', { exact: true })).toBeVisible();
  await page.getByLabel('搜索导出店铺', { exact: true }).fill('');
  await expect(page.locator('.admin-table .ant-table-row').first()).toBeVisible();
  const row = page.locator('.admin-table .ant-table-row').first(),
    height = (await row.boundingBox()).height;
  await row.getByRole('button', { name: /报表更多操作/ }).click();
  await expect(page.getByRole('menuitem', { name: '删除报表' })).toBeVisible();
  expect((await row.boundingBox()).height).toBe(height);
  await page.keyboard.press('Escape');
  await page.goto(base + '/?view=shops');
  await expect(page.locator('.shop-card').first()).toBeVisible({ timeout: 30000 });
  await page.locator('.shop-machine-heading button').first().click();
  await expect(page.locator('.shop-machine-heading button').first()).toHaveAttribute('aria-expanded', 'false');
  await page.locator('.shop-machine-heading button').first().click();
  await page
    .locator('.shop-card')
    .first()
    .getByRole('button', { name: /更多操作/ })
    .click();
  await expect(page.getByRole('menuitem', { name: '店铺详情 / 浏览器' })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.goto(base + '/?view=settings');
  await expect(page.getByRole('heading', { name: '系统设置', exact: true })).toBeVisible();
  await expect(page.locator('.settings-card .ant-select-content').first()).toContainText('数据变化后更新');
  await expect(page.getByRole('button', { name: '保存采集设置', exact: true })).toBeDisabled();
  await page.screenshot({ path: 'tmp/admin-verified-settings.png' });
  await page.goto(base + '/?view=sources');
  await expect(page.getByRole('heading', { name: '同步与数据源', exact: true })).toBeVisible();
  await expect(page.locator('.sync-overview')).toBeVisible({ timeout: 45000 });
  await page.getByLabel('数据源搜索', { exact: true }).fill('不存在的模块');
  await expect(page.getByText('当前筛选没有匹配数据源', { exact: true })).toBeVisible({ timeout: 30000 });
  await page.getByLabel('数据源搜索', { exact: true }).fill('');
  await page.screenshot({ path: 'tmp/admin-verified-sources.png' });
  for (const view of ['cost_tables', 'promotion_ledger', 'report_view']) {
    await page.goto(base + '/?view=' + view);
    await expect(page.locator('main.app-shell')).toHaveAttribute('data-admin', 'true');
    await expect(page.locator('h1').first()).toBeVisible();
  }
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.goto(base + '/?view=profit');
  await expect(page.locator('.admin-table')).toBeVisible({ timeout: 30000 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  await page.goto(base + '/?view=overview');
  await expect(page.locator('main.app-shell')).toHaveAttribute('data-admin', 'false');
  await expect(page.getByRole('heading', { name: /实时经营概览/ })).toBeVisible({ timeout: 30000 });
  expect(failures).toEqual([]);
});
