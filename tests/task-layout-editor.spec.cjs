const { test, expect } = require('@playwright/test');
const base = process.env.ECMP_UI_TEST_URL || 'http://127.0.0.1:3212';
test.use({ launchOptions: { channel: 'chrome' }, viewport: { width: 1280, height: 850 }, reducedMotion: 'reduce' });

test('task grouping drafts, atomic save, independent cadence and safe deletion', async ({ page, context }) => {
  test.setTimeout(90000);
  page.setDefaultTimeout(12000);
  test.skip(!process.env.ECMP_UI_TEST_PASSWORD, 'Requires local test authentication');
  expect(
    (await context.request.post(base + '/api/auth/login', { data: { username: process.env.ECMP_UI_TEST_USERNAME || 'testadmin', password: process.env.ECMP_UI_TEST_PASSWORD } })).ok(),
  ).toBeTruthy();
  let config = {
    frequency_version: 1,
    enabled: true,
    max_concurrent: 10,
    shop_concurrent: 1,
    max_active_shops: 2,
    browser_idle_seconds: 30,
    frequency_groups: [
      { id: 'realtime', name: '实时', seconds: 60, timeout: 40 },
      { id: 'catalog', name: '商品目录', seconds: 1800, timeout: 60 },
    ],
    task_modes: { pdd: { 'overview@base': 'realtime', 'products@base': 'catalog', 'orders@unpaid': 'off' } },
    task_step_catalog: [
      { id: 'overview@base', kind: 'overview', label: '实时概览-常规任务', platforms: ['pdd'], home: true },
      { id: 'products@base', kind: 'products', label: '商品列表-常规任务', platforms: ['pdd', 'taobao'], home: true },
      { id: 'orders@unpaid', kind: 'orders', label: '订单查询-待付款回查', platforms: ['pdd'], home: false },
    ],
    task_layout: {
      version: 1,
      groups: [
        { id: 'home', name: '首页经营指标' },
        { id: 'products', name: '商品目录与上新' },
        { id: 'orders', name: '未完结订单' },
      ],
      assignments: {
        pdd: { 'overview@base': 'home', 'products@base': 'products', 'orders@unpaid': 'orders' },
        taobao: { 'products@base': 'products' },
      },
    },
  };
  const ranges = ['ad_reports@yesterday', 'ad_reports@week', 'ad_reports@month'];
  config.task_step_catalog.push({
    id: 'ad_reports@history_query',
    kind: 'ad_reports',
    label: '推广报表-历史综合查询',
    platforms: ['pdd'],
    members: ranges,
    ranges: ['昨日数据', '近7天数据', '近30天数据'],
  });
  for (const id of ranges) {
    config.task_modes.pdd[id] = 'off';
    config.task_layout.assignments.pdd[id] = 'home';
  }
  const writes = [],
    errors = [];
  page.on('pageerror', (error) => {
    errors.push(error.message);
  });
  let rejectSave = false,
    offline = false;
  const settings = () => ({
    collection: config,
    scope_node_id: 1,
    configuration_read_only: offline,
    node_configs: [{ id: 1, name: '测试节点', online: !offline, collection: config }],
  });
  await page.route('**/api/**', async (route) => {
    const req = route.request(),
      url = new URL(req.url()),
      path = url.pathname;
    if (req.method() !== 'GET') {
      if (path === '/api/logs') return route.fulfill({ json: { ok: true } });
      expect(path).toBe('/api/system/settings');
      const body = req.postDataJSON();
      writes.push(body);
      if (rejectSave)
        return route.fulfill({
          status: 502,
          json: { detail: '测试节点未确认修改', results: [{ node: '测试节点', ok: false }] },
        });
      const layout = body.settings_patch?.task_layout_patch;
      if (layout) {
        const groups = new Map(config.task_layout.groups.map((g) => [g.id, g]));
        for (const [id, update] of Object.entries(layout.group_updates || {}))
          if (update) groups.set(id, { id, ...update });
          else groups.delete(id);
        config.task_layout.groups = [...groups.values()];
        for (const a of layout.step_assignments || [])
          config.task_layout.assignments[a.platform][a.step_id] = a.group_id;
        for (const assignment of Object.values(config.task_layout.assignments))
          for (const [step, parent] of Object.entries(assignment))
            if (layout.group_moves?.[parent]) assignment[step] = layout.group_moves[parent];
      }
      for (const a of body.module_assignments || []) config.task_modes[a.platform][a.kind] = a.target;
      config = structuredClone(config);
      return route.fulfill({ json: { ...settings(), results: [{ node: '测试节点', ok: true }] } });
    }
    if (path === '/api/connection')
      return route.fulfill({ json: { connected: true, center: true, cacheScope: 'task-layout-fixture' } });
    if (path === '/api/shops') return route.fulfill({ json: [] });
    if (path === '/api/nodes')
      return route.fulfill({ json: [{ id: 1, name: '测试节点', center: true, online: true }] });
    if (path === '/api/active-node') return route.fulfill({ json: { id: 1 } });
    if (path === '/api/events') return route.fulfill({ contentType: 'text/event-stream', body: ': fixture\n\n' });
    if (path === '/api/system/settings')
      return route.fulfill({
        json: {
          ...settings(),
          step_metrics: { pdd: { 'overview@base': { estimated_read_seconds: 12, sample_count: 2 } } },
          step_metrics_status: { complete: true },
        },
      });
    if (path === '/api/system/progress' || path === '/api/system/summary')
      return route.fulfill({ json: { collection: config, queues: {}, summary: {}, master_tasks: [] } });
    if (path === '/api/system/sources')
      return route.fulfill({ json: { rows: [], snapshot_complete: true, counts: {}, kinds: [], total: 0 } });
    return route.fulfill({ json: {} });
  });
  await page.goto(base + '/?view=sources');
  await page.getByRole('button', { name: /展开采集队列/ }).click({ timeout: 15000 });
  const editor = page.locator('.task-layout-settings');
  await editor.locator(':scope > summary').click();
  await expect(editor).toContainText('约 12 秒');
  await expect(editor.locator('[data-task-id="orders"]')).toContainText('已暂停');
  await editor.getByRole('button', { name: /新增后台任务/ }).click();
  await page.getByRole('textbox', { name: '后台任务名称', exact: true }).fill('自定义经营任务');
  await page.getByRole('button', { name: '加入修改草稿', exact: true }).click();
  const target = editor.locator('[data-task-id]').filter({ hasText: '自定义经营任务' });
  const source = editor.getByRole('button', { name: '选择子步骤 实时概览-常规任务', exact: true });
  await expect(page.getByRole('dialog')).toHaveCount(0);
  // Keep both ends visible before pressing the mouse. dragTo otherwise scrolls
  // after mousedown and may skip Chromium's native drag-start threshold.
  await target.scrollIntoViewIfNeeded();
  const sourceBox = await source.boundingBox(),
    targetBox = await target.boundingBox();
  await page.mouse.move(sourceBox.x + 16, sourceBox.y + 16);
  await page.mouse.down();
  await page.mouse.move(sourceBox.x + 40, sourceBox.y + 25, { steps: 8 });
  await page.mouse.move(targetBox.x + 80, targetBox.y + 80, { steps: 20 });
  await page.mouse.up();
  await expect(target).toContainText('实时概览-常规任务');
  expect(writes).toEqual([]);
  await editor.getByRole('tab', { name: /任务子步骤/ }).click();
  await expect(
    editor.getByRole('combobox', { name: '实时概览-常规任务所属任务', exact: true }).locator('..'),
  ).toContainText('自定义经营任务');
  await editor.getByRole('combobox', { name: '实时概览-常规任务更新频率', exact: true }).click();
  await page
    .locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content')
    .getByText('商品目录 · 每 30 分钟', { exact: true })
    .click();
  await editor.getByRole('combobox', { name: '推广报表-历史综合查询更新频率', exact: true }).click();
  await page
    .locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content')
    .getByText('商品目录 · 每 30 分钟', { exact: true })
    .last()
    .click();
  await editor.getByRole('button', { name: '保存任务设置', exact: true }).click();
  await expect(editor.getByRole('status')).toContainText('各节点已确认保存');
  expect(writes).toHaveLength(1);
  expect(writes[0].module_assignments).toEqual([
    { platform: 'pdd', kind: 'overview@base', tab: 'step', target: 'catalog' },
    ...ranges.map((kind) => ({ platform: 'pdd', kind, tab: 'step', target: 'catalog' })),
  ]);
  expect(writes[0].settings_patch.task_layout_patch.step_assignments).toHaveLength(1);
  expect(config.task_modes.pdd['orders@unpaid']).toBe('off');
  expect(config.browser_idle_seconds).toBe(30);
  await editor.getByRole('tab', { name: /后台任务/ }).click();
  await editor.locator('.task-layout-grid').scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'tmp/task-layout-desktop.png' });
  await editor.getByRole('button', { name: '商品目录与上新设置', exact: true }).click();
  await page.getByRole('menuitem', { name: '删除并转移子步骤', exact: true }).click();
  await page.getByRole('combobox', { name: '删除后迁入任务', exact: true }).click();
  await page
    .locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content')
    .getByText('首页经营指标', { exact: true })
    .click();
  await page.getByRole('button', { name: '加入修改草稿', exact: true }).click();
  expect(writes).toHaveLength(1);
  await editor.getByRole('button', { name: '保存任务设置', exact: true }).click();
  await expect(editor.getByRole('status')).toContainText('各节点已确认保存');
  expect(config.task_layout.assignments.taobao['products@base']).toBe('home');
  expect(config.task_modes.pdd['products@base']).toBe('catalog');
  rejectSave = true;
  await editor.getByRole('button', { name: '选择子步骤 订单查询-待付款回查', exact: true }).click();
  await editor.locator('[data-task-id="home"]').getByRole('button', { name: '移入此任务' }).click();
  await editor.getByRole('button', { name: '保存任务设置', exact: true }).click();
  await expect(editor.getByRole('status')).toContainText('未确认草稿已保留');
  await expect(editor.getByRole('button', { name: '保存任务设置', exact: true })).toBeEnabled();
  await page.setViewportSize({ width: 480, height: 720 });
  await expect(editor.locator('.task-layout-grid')).toBeVisible();
  expect(
    await editor.locator('.task-layout-grid').evaluate((e) => e.getBoundingClientRect().right <= innerWidth + 1),
  ).toBeTruthy();
  await page.screenshot({ path: 'tmp/task-layout-mobile.png' });
  await editor.getByRole('tab', { name: /任务子步骤/ }).click();
  const tableScroll = editor.locator('.ant-table-content');
  await expect(tableScroll).toBeVisible();
  expect(await tableScroll.evaluate((element) => element.scrollWidth > element.clientWidth)).toBeTruthy();
  expect(await tableScroll.evaluate((element) => element.getBoundingClientRect().right <= innerWidth + 1)).toBeTruthy();
  offline = true;
  await editor.getByRole('button', { name: '读取最新配置', exact: true }).click();
  await expect(editor).toContainText('节点离线，展示上次配置，启动采集服务后可保存。已有草稿保留。');
  await expect(editor.getByRole('button', { name: '保存任务设置', exact: true })).toBeDisabled();
  await expect(editor.getByRole('combobox', { name: '实时概览-常规任务所属任务', exact: true })).toBeDisabled();
  await expect(editor).toContainText('1 项未保存修改');
  await page.screenshot({ path: 'tmp/task-layout-mobile-steps.png' });
  expect(errors).toEqual([]);
});
