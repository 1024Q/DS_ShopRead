const { test, expect } = require('@playwright/test');
const base = process.env.ECMP_UI_TEST_URL || 'http://127.0.0.1:3212';
test.use({ launchOptions: { channel: 'chrome' }, reducedMotion: 'reduce' });
test('small windows keep dialogs and scrolling controls reachable', async ({ page, context }) => {
  test.setTimeout(180000);
  test.skip(!process.env.ECMP_UI_TEST_PASSWORD);
  expect(
    (await context.request.post(base + '/api/auth/login', { data: { username: process.env.ECMP_UI_TEST_USERNAME || 'testadmin', password: process.env.ECMP_UI_TEST_PASSWORD } })).ok(),
  ).toBeTruthy();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  for (const width of [480, 768, 1024]) {
    await page.setViewportSize({ width, height: 600 });
    await page.goto(base + '/?view=shops');
    await page.getByRole('button', { name: '管理运营人员 / 批量分配', exact: true }).click();
    await page.locator('.ant-modal').getByRole('button', { name: '店铺分配', exact: true }).click();
    await page.waitForTimeout(350);
    const bounds = await page.locator('.ant-modal').boundingBox();
    expect(bounds.y).toBeGreaterThanOrEqual(0);
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(600);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
    await page.locator('.ant-modal-body').evaluate((e) => (e.scrollTop = e.scrollHeight));
    const close = page.locator('.ant-modal').getByRole('button', { name: '关闭', exact: true });
    await expect(close).toBeInViewport();
    await close.click();
    await page.goto(base + '/?view=orders');
    await expect(
      page.locator('.admin-business-table').getByRole('button', { name: '详情', exact: true }).first(),
    ).toBeVisible({ timeout: 60000 });
    const host = page.locator('.admin-business-table .ant-table-content');
    await host.evaluate((e) => {
      e.scrollLeft = e.scrollWidth;
    });
    expect(await host.evaluate((e) => e.scrollLeft)).toBeGreaterThan(0);
    const issue = await page
      .locator('.filter-panel')
      .evaluate((e) =>
        [...e.querySelectorAll('input,.ant-select')].some((n) => n.getBoundingClientRect().right > innerWidth + 1),
      );
    expect(issue).toBeFalsy();
    if (width <= 768)
      expect(
        await page
          .locator('.admin-business-table .pinned-column')
          .first()
          .evaluate((e) => getComputedStyle(e).position),
      ).toBe('static');
    await page.screenshot({ path: `tmp/responsive-verified-${width}.png` });
  }
  expect(errors).toEqual([]);
});
