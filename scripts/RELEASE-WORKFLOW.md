# 测试提交和签名发布

从项目根目录运行 `测试提交并签名.cmd`，或使用 `.venv\Scripts\python.exe scripts\release-update.py`。必须明确指定提交说明和路径；脚本不会默认暂存整个项目。

先预览选中的文件（不测试、不暂存、不提交、不发布）：

```powershell
.\测试提交并签名.cmd --message "修复采集查询" --paths backend config app tests --dry-run
```

确认范围后执行：

```powershell
.\测试提交并签名.cmd --message "修复采集查询" --paths backend config app tests
```

也可以只指定文件；路径中含空格时加引号。新文件必须包含在路径范围内，删除文件也会纳入提交。已有其他暂存文件、冲突、重命名，以及范围外未提交的采集打包文件会阻止执行。三个本机公钥、版本和 EGO 试验文件不要顺带纳入。

执行顺序：

1. 检查路径、Git 作者信息及既有中心签名密钥。
2. 在临时数据目录运行后端发布门禁，再运行前端单元测试；任一步失败立即停止。
3. 如传入 `--build`，额外构建独立的 `.next-release-*` 目录。脚本打印目录，不自动切换后台构建配置。
4. 核对测试期间代码和 HEAD 没有变化，提交明确选择的文件；提交失败不发布。需要预先配置 Git 的 `user.name` 和 `user.email`。
5. 调用现有发布器，只复用本次刚通过的后端测试结果，生成签名代码补丁。使用原 `.data/center/update-signing.local.pem`；加密密钥通过环境变量 `ECMP_SIGNING_PASSPHRASE` 提供口令，不能放进命令参数或脚本。

可选 `--node-id <采集节点UUID>` 进行灰度发布，稳定发布索引不变。省略时全量发布代码更新。没有代码内容变化时，原发布器会保留现有版本。

该脚本不会生成整套运行库包、创建签名密钥、推送 Git、启动或重启服务。构建成功也不等于已部署网页。签名发布失败时，已经完成的 Git 提交会保留，不自动回滚；修复原因后按 `AGENTS.md` 使用原发布器重新验证和发布。

脚本仅测试本项目现有代码；涉及运行库或依赖变更时应先完成独立兼容升级。路径保护覆盖本地数据目录、浏览器资料、运行时目录及常见密钥文件，仍需检查提交差异，不能将它视作完整的内容泄密扫描。

脚本自身的最小验证：

```powershell
.venv\Scripts\python.exe tests\test_release_update.py
```

## 一键测试、发布并重启（release-and-restart.ps1）

`测试发布并重启.cmd` 调用 `scripts\release-and-restart.ps1`，在上述流程之后继续完成部署。路径用逗号分隔：

```powershell
# 先预览（不测试、不提交、不发布、不重启）
.\测试发布并重启.cmd -Message "静默采集与调试模式" -Paths "backend,app,lib,项目交接说明.md" -DryRun
# 正式执行
.\测试发布并重启.cmd -Message "静默采集与调试模式" -Paths "backend,app,lib,项目交接说明.md"
```

执行顺序：

1. 调用 `release-update.py`：隔离后端门禁 → 前端单测 →（网页有改动时）构建新的 `.next-release-*` → 提交所选文件 → 签名发布。任一步失败立即停止，不重启任何服务。
2. 中心：所选变更包含 `backend/`（测试除外）或 `config/` 时重启。只停止监听 8010、命令行为 `backend.center.server` 且归属本项目的进程，随后用 `start-center.ps1` 启动并等待 `/health`。
3. 网页：变更包含 `app/`、`lib/`、`public/`、`server.cjs`、`next.config.mjs`、`package*.json` 时切换。新构建先在 127.0.0.1:3212 预览验证，通过后写入 `.data\center\ui-build.local.json`（不产生 Git 变更），再重启 3211；失败自动恢复原构建目录并重启。
4. 采集端：发布了新版本时，读取 `.data\updates\status.json` 等待本机自动升级与试运行完成（默认最长 20 分钟）；状态出现“回退”即报失败。本机采集服务未运行时跳过。

常用参数：`-Center always|never`、`-Web always|never` 覆盖自动判断；`-SkipCollectorWait` 不等待采集端；`-CollectorTimeoutMinutes 30`；`-NodeId <UUID>` 灰度发布（目标非本机时不等待）；`-SkipRelease -Center always` 只重启中心；`-SkipRelease -Web always -WebDistDir .next-xxx` 只切换到指定构建。

退出码：0 成功；1 失败（详见日志）；2 结束时中心或网页健康检查未通过；3 采集端升级等待超时。日志写入 `.data\service-logs\release-<时间>.log`。同一时间只允许运行一个实例。脚本不会按进程名批量结束 Python/Node/Chrome，无法确认归属的端口占用会直接报错停止。
