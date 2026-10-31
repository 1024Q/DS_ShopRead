# Windows 中心部署

当前架构为 Next.js 后台（3211）、FastAPI 中心（8010）、PostgreSQL，以及独立采集节点。旧 3210 后台和 8000 查询服务已退役。

## 启动

双击根目录 `启动后台.cmd`，启动中心和网页后台；采集节点使用原 GUI 快捷方式单独启动。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start-backend-all.ps1 -CheckOnly
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start-backend-all.ps1
```

已有健康服务会跳过。端口占用但检查失败时停止启动流程，不自动结束未知进程。日志在 `.data/service-logs/`。中心管理登录使用原有密钥；免登录经营概览保持只读。

## 构建切换与回退

当前构建统一读取 `config/ui-build.json` 的 `dist_dir`；本机可用 `.data/center/ui-build.local.json` 覆盖。两个启动脚本共用 `scripts/ui-config.ps1`，无需分别修改目录名称。

构建到新目录，不能覆盖正在运行的目录：

```powershell
$env:PDD_NEXT_DIST_DIR='.next-new-release'
.\.runtime\node.exe node_modules/next/dist/bin/next build --webpack
.\scripts\start-center-ui.ps1 -Port 3212 -DistDir .next-new-release -ListenAddress 127.0.0.1
```

通过验证后，更新构建配置，核对并停止原 3211 网页进程，再运行启动脚本。回退时将配置指回保留的构建；采集端和中心数据库无需随网页回退。`-DistDir` 是单次覆盖，不修改配置。

## 可选 HTTPS

默认保持现有局域网 HTTP。后台和中心都支持 HTTPS，但必须先准备可信证书并部署客户端信任，不能直接强制切换。

在 `.data/center/listen.local.json` 中按需配置：

| 字段 | 用途 |
| --- | --- |
| `address` | 中心监听地址，`127.0.0.1` 或 `0.0.0.0` |
| `tls_cert`、`tls_key` | 中心 8010 证书和私钥路径 |
| `ui_tls_cert`、`ui_tls_key` | 网页 3211 证书和私钥路径 |
| `internal_url` | 网页访问中心的地址；启用中心 TLS 后使用证书匹配的 HTTPS 主机名 |
| `ca_file` | Node 进程信任的自有 CA 文件 |

一键启动按配置选择健康检查协议。默认 HTTPS 检查访问 localhost，因此证书应包含 localhost；如使用专用主机名，需对应调整入口和健康检查地址。Windows PowerShell 健康检查使用 Windows 证书信任库，`ca_file` 只为 Node 增加信任，不会自动导入 Windows 证书库。

网页只有实际启用 TLS 时才设置 Secure Cookie。`-LocalHttp` 保留为兼容参数，不能与网页 TLS 同时使用。没有证书时在全部网卡上监听会显示明文 HTTP 提示。现有节点的中心地址和信任设置不会被自动修改。

## 运行依赖

新建中心环境使用 `backend/center/requirements.lock`；采集运行库使用 `backend/requirements.lock`。锁文件基于已验证的 Windows x64 / Python 3.14 版本生成，不等同于已升级所有机器。

```powershell
python -m pip install -r backend/center/requirements.lock
python scripts/verify-dependencies.py --center
```

采集安装包构建会核对现有便携运行库与锁文件，不匹配则停止。运行库升级仍走独立兼容流程；普通签名代码补丁不安装依赖。

## 数据与凭据

必须保留 `.data`、登录目录、数据库、节点凭据、`credentials.key`、中心配置和既有升级签名私钥。此次优化没有迁移或重置任何凭据，也没有改变采集频率、并发、休眠及停用状态。

详细架构、GUI 启停、数据目录和统计口径见 [项目交接说明](项目交接说明.md)。

中心隧道注册表在进程内存中，必须使用单 worker。启动脚本与应用会拒绝 `WEB_CONCURRENCY`、`UVICORN_WORKERS` 或 `--workers` 大于 1；不要用多实例负载均衡代理同一个中心。横向扩容前需先实现共享隧道路由。
