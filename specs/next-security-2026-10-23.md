# Next.js 安全更新验证（2026-10-23）

- Next.js 从 16.4.0 升至 16.4.2，锁定精确版本；仅同时更新配套 @next/env 和 SWC 包。
- React、Ant Design、页面、业务接口、采集代码、频率和并发未因本次升级修改。
- npm 公告查询确认旧版本命中 GHSA-p3m5-59mp-jv5r、GHSA-2464-fhr4-w9xw、GHSA-673q-mjm6-r5xr。
- 安装后完整依赖审计与生产依赖审计均为 0 个已知漏洞（仅代表查询时的公告结果）。
- 生产构建 .next-security-16-4-2 成功，无原安全告警。
- 3212 预验证：店铺浏览器筛选和按店操作、480px 布局测试通过；真实中心鉴权、首页 6 张指标卡、店铺浏览器、报表导出加载测试通过。
- 正式 3211 切换后重复真实鉴权和主要页面冒烟测试通过。只重启网页 Node 进程，未重启采集服务或中心 Python 服务。
- 旧 tests/local-access.test.cjs 有 2 项失败：它仍期待本机免登录，而当前 remote-upgrade.cjs 始终要求中心 Cookie。该文件与本次依赖升级无关，未为通过旧断言放宽鉴权；以上真实 HTTP 401 / 登录测试验证当前行为。
- scripts/start-center-ui.ps1 默认构建目录已指向新版本。

公告：
- https://github.com/advisories/GHSA-p3m5-59mp-jv5r
- https://github.com/advisories/GHSA-2464-fhr4-w9xw
- https://github.com/advisories/GHSA-673q-mjm6-r5xr
