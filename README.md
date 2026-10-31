# 多店铺数据工作台

当前架构：Next.js 管理后台 + Python 中心 API + 独立 PostgreSQL + Windows 采集节点。

- [Windows 中心部署](WINDOWS-CENTER.md)
- [采集端部署与自动升级](COLLECTOR-QUICKSTART.md)
- [项目交接说明](项目交接说明.md)
- [代码优化清单与验收记录](待优化.md)

## 本机服务入口

- 双击根目录 `启动后台.cmd`，同时启动中心 API 和网页后台；已运行服务会跳过，采集节点仍单独启动。
- 管理后台：http://127.0.0.1:3211/center
- 登录首页免密码展示实时经营概览、优先关注、成交走势、上新统计及各店铺今日与昨日走势，复用后台组件和统计口径，每 15 秒刷新。管理店铺、明细数据和设置仍需点击“管理登录”并输入中心管理密钥。概览只读，不触发采集。
- 中心 API：8010，由 `scripts/start-center.ps1` 启动。
- 采集服务：8001，由 `scripts/start-collector.ps1` 启动。
- 节点代理：`scripts/start-agent.ps1 -LocalCenter`。
- 管理后台：`scripts/start-center-ui.ps1 -LocalHttp`。

原 3210 单机后台、8000 查询服务及 API 直连机器管理已退役。跨机器管理、汇总与远控统一通过中心节点连接完成。

页面读取中心数据库，采集节点按配置频率采集并上传。账号密码和浏览器登录资料保留在采集节点；迁移店铺时按用户选择转发已保存密码，不在中心持久保存。

## 文件与数据

`.data` 保存配置、数据库、密钥、节点状态与日志；`.data/profiles` 和 `.pdd-chrome-profile` 保存店铺登录资料。不要将这些目录作为临时文件清理。备份恢复须同时保留数据库及其配套密钥。

`app` 为管理界面，`backend/center` 为中心服务，`backend` 为采集和共享业务代码。`scripts` 包含部署、发布及历史检查工具；历史检查脚本保留，部分针对已退役功能，不应直接作为当前验收依据。

旧版 Node.js 单机管理器及其专用测试、示例、截图工具和 5 个旧单机诊断脚本已删除。当前后台构建由 `config/ui-build.json` 统一指定，可用 `.data/center/ui-build.local.json` 覆盖，保留旧构建供回退。构建目录可再生成，业务数据和登录资料不属于构建缓存。

采集代码更新使用 `scripts/publish-collector-update.py` 发布签名补丁；完整采集安装包只用于新机器首次部署。

## 开发检查

- `npm run lint`：ESLint 与 Ruff 正确性检查；Ruff 安装自 `requirements-dev.txt`。
- `npm run test:web`：自动发现前端 Node 单元测试。
- `npm run test:python`：本次修改相关的最小后端回归测试。
- `npm run test:all`：前后端上述检查组合，不代替采集发布要求的完整后端测试。
- `npm run test:browser`：手动运行公开首页浏览器测试；设置 `ECMP_UI_TEST_URL` 指向已启动的预览服务，管理登录场景仅在提供测试令牌时运行。
- `npm run format:files -- <明确文件路径>`：格式化指定前端文件。
- `npm run format:web` / `npm run format:python`：按统一配置格式化前端 / Python 源码。
- `npm run format:check`：只检查格式，不修改文件；CI 同样执行。

## 前端目录与样式

`app/` 根目录只保留路由入口 `page.js`、`layout.js`，API 和 `/center` 路由位置不变。

| 目录 | 内容 |
| --- | --- |
| `app/components/overview` | 首页指标、预警、趋势和上新 |
| `app/components/reconciliation` | 报表、成本、利润和税费 |
| `app/components/shops` | 店铺管理和浏览器管理 |
| `app/components/collection` | 采集任务、频率、队列、覆盖情况 |
| `app/components/settings` | 系统、节点、数据库设置 |
| `app/components/business` | 订单、售后、商品等业务列表 |
| `app/components/shared`、`shell` | 通用控件、导航和工作区外壳 |
| `app/providers`、`hooks`、`lib` | 上下文、自定义 hooks、纯工具函数和数据读取 |
| `app/styles` | 按明确顺序加载的全局基础、布局、主题和 Ant Design 样式 |

全局 CSS 统一从 `app/styles/index.css` 进入，不要自动排序其中的导入；后面的主题规则有意覆盖前面的基础规则。多选框和状态标签使用同目录 CSS Modules。新增独立控件优先使用 CSS Modules，避免继续增加 `*-patch`、`*-polish` 覆盖文件。此次保留现有全局布局和 Ant Design 覆盖规则，未强行改写它们的优先级。

纯格式化提交为 `f599604ce1b871b93032b035fba018fa3b967a8e`，已记录在 `.git-blame-ignore-revs`。查看历史可使用 `git blame --ignore-revs-file .git-blame-ignore-revs <文件>`。组件移动直接更新引用，没有保留旧路径转发文件。

CI 在 Windows / Python 3.14 / Node 22 上执行 lint、最小回归和构建。仓库分支保护需要仓库管理员将 Checks 设置为必需检查；本地添加工作流不等于已开启远程保护。
