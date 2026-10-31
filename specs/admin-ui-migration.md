# 后台 Ant Design 改造

## 范围

- Ant Design 6.6.6、Next.js Registry 1.3.0，版本由 package-lock.json 固定。
- AdminTheme 统一主题，响应已有暗色设置；首页继续使用原布局。
- 对账中心 DataTable 使用 Ant Design Table、筛选弹层、导出菜单、列设置与导出记录抽屉。
- 店铺管理、同步与数据源、系统设置使用统一控件，原接口、事件处理、并发与频率不变。
- AdminControls 保持旧受控输入的事件契约；提交按钮显式声明 submit，保存期间继承禁用状态。
- 原全局 CSS 排除 Ant Design 元素，避免旧 input 背景覆盖选择框文字。新增业务布局限定在 data-admin=true。
- 金额仍按既有服务端逻辑核算；未匹配成本、成本版本、批量删除、上传均保留。

## 清理依据

| 文件或目录 | 处理及依据 |
| --- | --- |
| app/CollectionPriority.js | 删除；全仓引用扫描仅发现自身，旧优先级展示已退役 |
| public/index.html、public/app.js | 删除；退役静态后台，当前 server.cjs 使用 Next.js 路由，无当前入口引用 |
| 旧 DataTable、ReportTaskActions 样式 | 随组件替换删除，避免旧菜单撑高表格 |
| ReportExports、ReportView 中旧表格样式 | 删除已经失去对应 DOM 的选择器 |
| CostTables 旧分页读取 effect | 删除；明细已由 DataTable 读取，旧 effect 的结果无人使用 |
| 历史 .next-* 构建 | 仅在新版上线后清理；保留 .next-admin-antd-v3 与 .next-table-tools 回退构建 |
| .data、浏览器资料、签名密钥、发布包、运行时、数据库 | 保留，不属于可随意清理的临时产物 |
| 手工维护脚本、旧测试与其依赖 | 有显式调用或测试引用的保留，不能仅因修改时间早就删除 |

## 验证

- 最小化 Node 检查：table-tools、frequency-ui、source-display。
- 最小化 Python 检查：table_tools、cost_supplements、profit。
- tests/admin-ui.spec.cjs：登录后只读浏览页面、排序、搜索、勾选、菜单、抽屉、折叠及窄窗口；不保存采集配置或删除业务记录。
- 测试环境变量 ECMP_UI_TEST_URL 指向本地预览，ECMP_UI_TEST_TOKEN 由本地配置安全注入，不保存至源码。
- 部署入口 scripts/start-center-ui.ps1 默认使用 .next-admin-antd-v3；回退可显式指定 -DistDir .next-table-tools。

本次只发布网页后台，不发布采集端补丁、不重启采集节点。

## Remaining business pages (2026-10-16)
- Reuse Ant Design controls across orders, aftersales, products, promotion, tasks, logs, fleet and center management. Preserve serialized filter values and original form submit handlers.
- `AdminWorkspaceContext` keeps home controls and home table rendering unchanged. `HomeSortableTable` is intentionally retained for that compatibility boundary.
- Shared `SortableTable` uses Ant Table headers and sorting. Composite business rows retain their React component lifecycle and grouped expansion; never invoke business row components as plain functions or discard multi-cell children.
- `AdminDialog` delegates close to the original guarded action. Disabled busy actions and unsaved-input confirmation remain active; Ant owns portal focus and keyboard trapping.
- Consolidate business dialog/table styling in `admin-ui.css`; remove obsolete report confirmation overlay and scoped native preview-table/control declarations.
- Validation: production build, focused sorting/frequency/source display unit checks and read-only Playwright workflows (orders/detail, operator dialog, source groups/detail, promotion, goods, logs, fleet, existing reconciliation flows, 1024 px overflow and home isolation).
- No backend API contracts, database records, scheduler, frequencies or collector releases change in this UI update.

Final validation: production build `.next-admin-unified-v7` succeeded; 6 focused Node checks passed; both Playwright suites passed (38.8 seconds), including business-column preservation and sticky header/body alignment. Previous production build `.next-admin-antd-v3` is retained for rollback.

## Responsive follow-up (2026-10-16)
- Confirmed long dialogs exceeded a 600px-high viewport because of their top offset; center them and constrain the scrolling body to the available dynamic viewport height. Keep the original guarded close action sticky and accessible.
- At table container widths below 720px, release pinned columns so they cannot cover all middle columns. Keep horizontal scrolling inside the table.
- Constrain narrow filter controls and allow dialog toolbars to wrap. Home styles and business handlers remain unchanged.
- Validation: production build and two read-only Playwright suites passed (48.2s); checked 480/768/1024px widths, 600px height, dialog bounds, close after scrolling, filter bounds, horizontal scrolling and business regression. Visually verified the small-window dialog screenshot.
