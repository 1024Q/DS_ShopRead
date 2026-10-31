# 首页 Ant Design 与 PySide6 控制台验证

日期：2026-10-16

## 改动边界

- 首页组件统一为 Ant Design，保留看板配色、趋势计算、缺失值和预警跳转过滤条件。
- 未修改采集频率、调度规则、业务统计及数据库格式。
- PySide6 使用原 Runner、接入审批、DPAPI、并发 API、日志读取和签名更新服务。
- Qt 运行库独立安装；缺少 Qt 的节点继续使用兼容控制台。

## 测试结果

- 生产构建成功；浏览器测试覆盖首页、对账、店铺、同步、设置、业务表格、弹窗和 480 / 768 / 1024 / 1440 像素窗口。
- 前端逻辑测试 16 项通过。
- Qt 控制台与现有启动、日志、并发、审批、自动更新相关测试共 64 项通过。
- 全量逐文件隔离运行 141 个后端测试文件：初次 90 通过、50 失败、1 超时。
- 将失败和超时的 51 个文件放到改造前 HEAD 版本复跑，全部复现同类失败/超时。
- 其中本次相关的并发配置测试仍要求已废弃的队列额度，已按现有节点/店铺两层配置修正测试；4 项复跑通过，未修改并发实现。
- 因而仍有 49 个原有失败文件、1 个原有超时文件；不能将本次结果表述为全量测试通过。

## 原有失败清单

| 测试文件 | 改造前结果 | 本次隔离结果 |
|---|---|---|
| test_attention.py | failed | failed |
| test_audit_store.py | failed | failed |
| test_browser_slot_wait.py | failed | failed |
| test_collection_and_nodes.py | failed | failed |
| test_collection_optimization.py | failed | failed |
| test_collection_priority.py | failed | failed |
| test_collection_queue.py | failed | failed |
| test_collection_recovery.py | failed | failed |
| test_collector_concurrency.py | failed | 修正过期断言后通过 |
| test_core_priority.py | failed | failed |
| test_database_reads.py | failed | failed |
| test_datalayer_fixes.py | failed | failed |
| test_date_collection.py | failed | failed |
| test_eight_optimizations.py | timeout | timeout |
| test_fleet_and_consolidation.py | failed | failed |
| test_focus_resolution.py | failed | failed |
| test_history_batches.py | failed | failed |
| test_import_and_status.py | failed | failed |
| test_master_tasks.py | failed | failed |
| test_module_assignment.py | failed | failed |
| test_node_configuration.py | failed | failed |
| test_operations.py | failed | failed |
| test_operations_insights.py | failed | failed |
| test_operator_arrivals.py | failed | failed |
| test_optimization_audit.py | failed | failed |
| test_order_plan.py | failed | failed |
| test_order_retry_scope.py | failed | failed |
| test_priority_rules.py | failed | failed |
| test_queue_control.py | failed | failed |
| test_read_architecture.py | failed | failed |
| test_read_budget.py | failed | failed |
| test_read_performance.py | failed | failed |
| test_recovery.py | failed | failed |
| test_remote_control.py | failed | failed |
| test_report_delete.py | failed | failed |
| test_report_export_diagnostics.py | failed | failed |
| test_report_incremental_guard.py | failed | failed |
| test_report_storage.py | failed | failed |
| test_requested_table_tax_fixes.py | failed | failed |
| test_retry_recovery.py | failed | failed |
| test_runtime_fixes.py | failed | failed |
| test_shop_concurrency.py | failed | failed |
| test_table_index.py | failed | failed |
| test_taobao.py | failed | failed |
| test_task_lifecycle.py | failed | failed |
| test_tax_change_actions.py | failed | failed |
| test_three_queues.py | failed | failed |
| test_unified_demands.py | failed | failed |
| test_update_gate.py | failed | failed |
| test_workspace.py | failed | failed |
| test_workspace_updates.py | failed | failed |

## Qt 运行库与回退

运行库包：`dist/ECMP-Qt6-Runtime-6.10.2.zip`，附 SHA-256 校验文件。
在控制台退出后解压到采集端根目录，新增 `.runtime/qt6-6.10.2`。配套代码支持自动识别 Qt。
本机已验证便携 Python 加载 Qt；远程机器是否安装运行库需分别核实。
回退运行库时退出控制台并改名该版本目录，保留原 Python、数据库、账号和浏览器资料。

## 发布核实

- 正式后台端口 3211 已切换至 `.next-home-antd-release`，线上首页与小窗口测试 2 项通过。
- 兼容采集端签名更新：`20261015204414-e4ff09d0bf`。
- 本机控制台进程由 35852 自动切换为 59360，已验证加载 Qt6Widgets.dll；本机健康接口返回 HTTP 200。
- 原有 5 分钟试运行机制继续执行，核实时 failures=0、restarts=0；不将试运行状态视为所有节点均已完成升级。
- 远程机器仍需安装 Qt 运行库扩展，未安装者继续使用兼容控制台。
- Qt 运行库 ZIP 的 SHA-256：`25e65a640262e6b46ff0b45d4865405583a900f7d1e8e2139ca0e6fac1aa2024`。
