const { test, expect } = require('@playwright/test');
const base = process.env.ECMP_UI_TEST_URL || 'http://127.0.0.1:3212';
test.use({ launchOptions: { channel: 'chrome' }, viewport: { width: 1440, height: 1000 } });

test('summary-only pages preserve metrics and request details only on explicit action', async ({ page, context }) => {
  test.setTimeout(90000);
  test.skip(!process.env.ECMP_UI_TEST_PASSWORD, 'Requires local test authentication');
  expect(
    (
      await context.request.post(base + '/api/auth/login', {
        data: { username: process.env.ECMP_UI_TEST_USERNAME || 'testadmin', password: process.env.ECMP_UI_TEST_PASSWORD },
      })
    ).ok(),
  ).toBeTruthy();
  const errors = [],
    commands = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const now = Date.now() / 1000;
  const shop = {
    id: 2,
    shop_id: 2,
    name: '摘要测试店',
    shop_name: '摘要测试店',
    platform: 'pdd',
    state: 'logged_in',
    operators: [],
  };
  const source = {
    ...shop,
    updated_at: now,
    summary_updated_at: now,
    summary_complete: true,
    details_on_demand: true,
    details_updated_at: null,
    details_complete: false,
    complete: false,
  };
  await page.route('**/api/**', async (route) => {
    const request = route.request(),
      url = new URL(request.url());
    if (request.method() !== 'GET') {
      commands.push({ path: url.pathname, force: url.searchParams.get('force') });
      return route.fulfill({ json: { accepted: true } });
    }
    if (url.pathname === '/api/shops') return route.fulfill({ json: [shop] });
    if (url.pathname === '/api/events')
      return route.fulfill({ contentType: 'text/event-stream', body: ': fixture\n\n' });
    if (url.pathname === '/api/data/transactions')
      return route.fulfill({
        json: {
          scope: { trade_range: 'realtime' },
          sources: [source],
          rows: [],
          total: 0,
          page_count: 1,
          trade_totals: { sales: 1234.56, orders: 8, buyers: 7 },
          metrics: [
            {
              ...source,
              realtime: true,
              range: 'realtime',
              summary: { sales: 1234.56, orders: 8, buyers: 7 },
              trend: [
                {
                  id: 'today-0',
                  date: '2026-10-29',
                  series: '今日累计',
                  hour: 0,
                  sales: 1234.56,
                  orders: 8,
                  buyers: 7,
                },
              ],
            },
          ],
        },
      });
    if (url.pathname === '/api/data/ad_reports')
      return route.fulfill({
        json: {
          scope: { trade_range: 'realtime' },
          sources: [{ ...source, details_updated_at: now - 3600, details_complete: true }],
          rows: [{ ...shop, id: 'saved-product', name: '之前采集的推广商品', ad_spend: 12, ad_revenue: 40 }],
          metrics: [{ ...shop, ad_spend: 25, total_spend: 30, ad_revenue: 100, daily: [] }],
          ad_totals: { ad_spend: 25, ad_revenue: 100, roi: 4, net_roi: 3 },
          total: 1,
          page_count: 1,
        },
      });
    return route.continue();
  });
  await page.goto(
    base +
      '/?view=transactions&filters=' +
      encodeURIComponent(JSON.stringify({ trade_range: 'realtime', shop_id: '2' })),
  );
  const trade = page.locator('.transaction-data');
  await expect(trade).toBeVisible();
  await expect(trade).toContainText('1,234.56');
  await expect(trade).toContainText('附属统计按需采集');
  await expect(trade).toContainText('尚未采集年度经营数据');
  await expect(page.locator('.data-coverage').first()).toContainText('汇总已读取 1 / 1 家');
  expect(commands).toEqual([]);
  await trade.getByText('查看各店铺明细更新时间', { exact: true }).click();
  await expect(trade).toContainText('尚未采集明细');
  await trade.getByRole('button', { name: '采集当前筛选店铺的附属统计', exact: true }).click();
  await expect
    .poll(() => commands.filter((command) => command.path === '/api/data/transactions/sync'))
    .toEqual([{ path: '/api/data/transactions/sync', force: 'true' }]);
  await page.goto(
    base + '/?view=ad_reports&filters=' + encodeURIComponent(JSON.stringify({ trade_range: 'realtime', shop_id: '2' })),
  );
  const ads = page.locator('.promotions-panel');
  await expect(ads).toContainText('商品推广明细按需采集');
  await expect(ads).toContainText('之前采集的推广商品');
  await expect(page.locator('.data-coverage').first()).toContainText('完整明细 1 / 1 家');
  expect(commands.filter((command) => command.path === '/api/data/ad_reports/sync')).toEqual([]);
  await ads.getByRole('button', { name: '采集当前筛选店铺的商品推广明细', exact: true }).click();
  await expect
    .poll(() => commands.filter((command) => command.path === '/api/data/ad_reports/sync'))
    .toEqual([{ path: '/api/data/ad_reports/sync', force: 'true' }]);
  expect(errors).toEqual([]);
});
