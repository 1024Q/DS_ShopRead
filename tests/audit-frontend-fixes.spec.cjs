const { test, expect } = require('@playwright/test');
const base = process.env.ECMP_UI_TEST_URL || 'http://127.0.0.1:3212';
test.use({ launchOptions: { channel: 'chrome' }, viewport: { width: 1440, height: 1000 } });

async function mockApp(page, context, handler) {
  test.skip(!process.env.ECMP_UI_TEST_PASSWORD, 'A test account is required');
  expect((await context.request.post(base + '/api/auth/login', { data: { username: process.env.ECMP_UI_TEST_USERNAME || 'testadmin', password: process.env.ECMP_UI_TEST_PASSWORD } })).ok()).toBeTruthy();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    if (await handler(route, url)) return;
    const defaults = {
      '/api/connection': { center: true, connected: true, cacheScope: 'audit-test', user: { id: 'test', role: 'admin', permissions: {} } },
      '/api/shops': [],
      '/api/nodes': [],
      '/api/active-node': { id: 0 },
      '/api/system/summary': { summary: { running: 0, queued: 0 } },
      '/api/cost-tables': { books: [] },
      '/api/table-tools/exports': [],
    };
    await route.fulfill({ json: defaults[url.pathname] ?? {} });
  });
  return errors;
}

test('recalculation refreshes detail rows; failed filters cannot expose or export stale rows', async ({
  page,
  context,
}) => {
  let version = 1,
    tableReads = 0,
    exportReads = 0;
  const id = 'a'.repeat(32),
    doc = {
      id,
      shop_id: 1,
      shop_name: '测试店铺',
      created: 1790000000,
      row_count: 1,
      start: '2026-09-01 00:00:00',
      end: '2026-10-01 00:00:00',
    };
  const errors = await mockApp(page, context, async (route, url) => {
    if (url.pathname === '/api/report-exports') {
      await route.fulfill({ json: { jobs: [doc] } });
      return true;
    }
    if (url.pathname === '/api/report-exports/view') {
      expect(url.searchParams.get('metadata_only')).toBe('true');
      await route.fulfill({
        json: {
          total: 1,
          statuses: ['已完成'],
          reports: [doc],
          accounting: [
            {
              job_id: id,
              updated: version,
              income_mode: 'row',
              summary: {
                shop_name: '测试店铺',
                start: doc.start,
                rows: 1,
                matched: 1,
                complete: true,
                book_name: '当前成本',
                product_cost: version * 10,
              },
            },
          ],
        },
      });
      return true;
    }
    if (url.pathname.endsWith('/recalculate')) {
      version++;
      await route.fulfill({ json: { ok: true } });
      return true;
    }
    if (url.pathname === '/api/table-tools/orders') {
      tableReads++;
      const body = route.request().postDataJSON();
      if (body.keyword === '失败') {
        await route.fulfill({ status: 503, json: { detail: '测试查询暂不可用' } });
        return true;
      }
      await route.fulfill({
        json: {
          columns: [
            { id: 'shop_name', label: '店铺名称' },
            { id: 'product', label: '商品' },
            { id: 'cost_total', label: '商品成本', type: 'number' },
          ],
          rows: [{ id: id + ':1', shop_name: '测试店铺', product: '有效商品', cost_total: version * 10 }],
          total: 1,
          summary: { cost_total: { value: version * 10, missing: 0 } },
        },
      });
      return true;
    }
    if (url.pathname === '/api/table-tools/exports') {
      exportReads++;
      await route.fulfill({ json: [] });
      return true;
    }
    return false;
  });
  await page.goto(base + '/?view=report_view&report_month=2026-09');
  const table = page.getByRole('region', { name: '详细报表', exact: true });
  await expect(table.getByRole('cell', { name: '有效商品', exact: true })).toBeVisible();
  await expect(table.getByRole('cell', { name: '10', exact: true })).toBeVisible();
  await page.getByText('店铺月度核算与成本版本', { exact: true }).click();
  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('button', { name: '使用当前成本重新核算', exact: true }).click();
  await expect(table.getByRole('cell', { name: '20', exact: true })).toBeVisible();
  expect(tableReads).toBeGreaterThan(1);
  await table.getByLabel('详细报表搜索').fill('失败');
  await expect(table.getByText('测试查询暂不可用', { exact: true })).toBeVisible();
  await expect(table.getByRole('cell', { name: '有效商品', exact: true })).toHaveCount(0);
  await expect(table.getByRole('button', { name: /导出$/ })).toBeDisabled();
  expect(exportReads).toBe(0);
  await table.getByRole('button', { name: '导出记录', exact: true }).click();
  await expect.poll(() => exportReads).toBeGreaterThan(0);
  expect(errors).toEqual([]);
});

test('unmatched costs defaults to current reports and history uses matching template scope', async ({
  page,
  context,
}) => {
  const scopes = [];
  const errors = await mockApp(page, context, async (route, url) => {
    if (url.pathname !== '/api/cost-supplements') return false;
    const scope = url.searchParams.get('scope');
    scopes.push(scope);
    const item = {
      sku: scope === 'history' ? 'OLD-SKU' : 'CURRENT-SKU',
      products: ['测试商品'],
      specs: ['单瓶'],
      shops: ['测试店铺'],
      rows: 1,
      reasons: ['缺失成本'],
      reports: [{ job_id: 'a'.repeat(32), month: '2026-09', shop_name: '测试店铺', created: 1790000000 }],
    };
    await route.fulfill({ json: { items: [item], jobs: [], sku_links: [] } });
    return true;
  });
  await page.goto(base + '/?view=cost_tables');
  await expect(page.getByRole('cell', { name: /^CURRENT-SKU/ })).toBeVisible();
  expect(scopes[0]).toBe('current');
  await expect(page.getByRole('link', { name: /导出当前生效未匹配SKU/ })).toHaveAttribute(
    'href',
    '/api/cost-supplements/template?scope=current',
  );
  await page.getByLabel('未匹配成本报表范围').click();
  await page.getByTitle('全部历史报表', { exact: true }).click();
  await expect(page.getByRole('cell', { name: /^OLD-SKU/ })).toBeVisible();
  await expect(page.getByRole('cell', { name: /^CURRENT-SKU/ })).toHaveCount(0);
  await expect(page.getByRole('link', { name: /导出全部历史未匹配SKU/ })).toHaveAttribute(
    'href',
    '/api/cost-supplements/template?scope=history',
  );
  expect(errors).toEqual([]);
});

test('all nodes action rebuilds the connection and event stream', async ({ page, context }) => {
  let active = 2,
    connections = 0,
    eventConnections = 0;
  const errors = await mockApp(page, context, async (route, url) => {
    if (url.pathname === '/api/active-node') {
      if (route.request().method() === 'POST') active = route.request().postDataJSON().id;
      await route.fulfill({ json: { id: active } });
      return true;
    }
    if (url.pathname === '/api/connection') {
      connections++;
      await route.fulfill({ json: { center: true, connected: true, cacheScope: 'node:' + active } });
      return true;
    }
    if (url.pathname === '/api/events') {
      eventConnections++;
      await route.fulfill({ contentType: 'text/event-stream', body: 'event: database\ndata: {"version":1}\n\n' });
      return true;
    }
    return false;
  });
  await page.goto(base + '/?view=browser_workspace');
  await expect(page.getByRole('heading', { name: '店铺浏览器工作台' })).toBeVisible();
  await expect.poll(() => eventConnections).toBeGreaterThan(0);
  const before = eventConnections;
  await page.getByRole('button', { name: '查看全部节点', exact: true }).click();
  await expect.poll(() => active).toBe(0);
  await expect.poll(() => connections).toBeGreaterThan(1);
  await expect.poll(() => eventConnections).toBeGreaterThan(before);
  expect(errors).toEqual([]);
});
