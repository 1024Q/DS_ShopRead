const { test, expect } = require('@playwright/test');
const base = process.env.ECMP_UI_TEST_URL || 'http://127.0.0.1:3212';
test.use({ actionTimeout: 15000, viewport: { width: 1440, height: 960 }, launchOptions: { channel: 'chrome' } });
test('remaining business UI: filters, sorted tables, dialogs and preserved home', async ({ page, context }) => {
  test.setTimeout(240000);
  test.skip(!process.env.ECMP_UI_TEST_PASSWORD, 'Requires local test authentication');
  expect(
    (await context.request.post(base + '/api/auth/login', { data: { username: process.env.ECMP_UI_TEST_USERNAME || 'testadmin', password: process.env.ECMP_UI_TEST_PASSWORD } })).ok(),
  ).toBeTruthy();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(base + '/?view=orders');
  await expect(page.getByRole('heading', { name: '订单查询', exact: true })).toBeVisible();
  await expect(page.locator('.filter-panel .ant-select').first()).toBeVisible();
  await expect(
    page.locator('.admin-business-table .ant-table-tbody>tr:not(.ant-table-measure-row)').first(),
  ).toBeVisible({ timeout: 60000 });
  await expect(
    page.locator('.admin-business-table').getByRole('button', { name: '详情', exact: true }).first(),
  ).toBeVisible({ timeout: 60000 });
  expect(
    await page
      .locator('.admin-business-table .ant-table-tbody>tr:not(.ant-table-measure-row)')
      .first()
      .locator('td')
      .count(),
  ).toBeGreaterThan(6);
  await page.locator('.admin-business-table .ant-table-thead').getByText('下单时间', { exact: true }).click();
  await expect(page.locator('.admin-business-table th[aria-sort]')).not.toHaveCount(0);
  await page.locator('.admin-business-table').getByRole('button', { name: '详情', exact: true }).first().click();
  await expect(page.locator('.ant-modal')).toBeVisible();
  await page.locator('.ant-modal').getByRole('button', { name: '关闭', exact: true }).click();
  await expect(page.locator('.ant-modal')).toBeHidden();
  const alignment = await page.locator('.admin-business-table table').evaluate((t) => {
    const heads = [...t.tHead.rows[0].cells],
      row = [...t.tBodies[0].rows].find(
        (r) => !r.classList.contains('ant-table-measure-row') && r.cells.length === heads.length,
      );
    return heads.flatMap((h, i) =>
      h.classList.contains('pinned-column') && !h.hidden
        ? [Math.abs(h.getBoundingClientRect().x - row.cells[i].getBoundingClientRect().x)]
        : [],
    );
  });
  expect(alignment.length).toBeGreaterThan(0);
  expect(alignment.every((delta) => delta < 2)).toBeTruthy();
  await page.screenshot({ path: 'tmp/admin-v4-orders.png' });
  await page.goto(base + '/?view=shops');
  await page.getByRole('button', { name: '管理运营人员 / 批量分配', exact: true }).click();
  await expect(page.locator('.ant-modal')).toContainText('运营人员管理');
  await page.locator('.ant-modal').getByRole('button', { name: '关闭', exact: true }).click();
  await page.goto(base + '/?view=sources');
  await expect(page.locator('.source-queue-heading').first()).toBeVisible({ timeout: 60000 });
  await page.locator('.admin-business-table').getByRole('button', { name: /详情/ }).first().click();
  await expect(page.locator('.ant-modal')).toContainText('采集详情');
  await page.locator('.ant-modal').getByRole('button', { name: '关闭采集详情' }).click();
  await page.screenshot({ path: 'tmp/admin-v4-sources.png' });
  for (const [view, title] of [
    ['ad_products', '商品推广'],
    ['goodsdata', '商品数据'],
    ['logs', '运行与错误日志'],
    ['fleet', '跨机器汇总'],
  ]) {
    await page.goto(base + '/?view=' + view);
    await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
    await expect(page.locator('.filter-panel .ant-select').first()).toBeVisible();
  }
  await page.goto(base + '/center');
  await expect(page.locator('main')).toBeVisible();
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.goto(base + '/?view=orders');
  await expect(page.locator('.admin-business-table')).toBeVisible({ timeout: 45000 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  await page.goto(base + '/?view=overview');
  await expect(page.locator('main')).toHaveAttribute('data-admin', 'false');
  await expect(page.getByRole('heading', { name: /实时经营概览/ })).toBeVisible();
  expect(errors).toEqual([]);
});
