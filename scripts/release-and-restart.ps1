[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$NodeId = '',
    [ValidateSet('auto', 'always', 'never')][string]$Center = 'auto',
    [ValidateSet('auto', 'always', 'never')][string]$Web = 'auto',
    [int]$CollectorTimeoutMinutes = 20,
    [switch]$SkipCollectorWait,
    [switch]$SkipRelease,
    [string]$WebDistDir = '',
    [switch]$DryRun
)
# 一键：测试 -> 签名发布 -> 按需重启中心/切换网页 -> 等待本机采集端自动升级验收。
# 不做任何 Git 提交/暂存（按用户要求）：直接发布工作区当前代码，Git 只用于只读判断哪些部分有改动。
# 安全约束：只停止“监听指定端口且命令行属于本项目”的进程；不按进程名批量结束；网页先在预览端口验证再切换，失败自动回退。
$ErrorActionPreference = 'Stop'
# 英文系统控制台默认 OEM 代码页（如 437）：Python 打印中文会报 'charmap' codec 错误，
# Git 的 UTF-8 输出也会显示成乱码。本进程及子进程统一使用 UTF-8。
$utf8 = [Text.UTF8Encoding]::new($false)
try { [Console]::OutputEncoding = $utf8 } catch { }
$OutputEncoding = $utf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$root = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $root
. (Join-Path $PSScriptRoot 'ui-config.ps1')

$python = Join-Path $root '.venv\Scripts\python.exe'
$logs = Join-Path $root '.data\service-logs'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$stamp = Get-Date -Format yyyyMMdd-HHmmss
$transcript = Join-Path $logs "release-$stamp.log"
Start-Transcript -LiteralPath $transcript | Out-Null

function Step($text) { Write-Host ''; Write-Host "==== $text ====" -ForegroundColor Cyan }
function Info($text) { Write-Host $text }
function Warn($text) { Write-Warning $text }
function Read-Json($path) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try { return Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $null }
}
# Windows PowerShell 5.1 在 Stop 模式下会把原生程序的 stderr 当成异常，原生命令统一在此执行。
function Native([scriptblock]$block) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $block } finally { $ErrorActionPreference = $previous }
}
function Git-Lines([string[]]$arguments) {
    $safe = 'safe.directory=' + ($root -replace '\\', '/')
    $output = Native { & git -c core.quotepath=false -c $safe @arguments 2>$null }
    if ($LASTEXITCODE -ne 0) { throw "git $($arguments -join ' ') 失败" }
    return @($output | Where-Object { $_ })
}

# ---------- 地址与健康检查（与 start-backend-all.ps1 相同的规则） ----------
$listen = Read-Json (Join-Path $root '.data\center\listen.local.json')
$centerUrl = 'http://127.0.0.1:8010'
if ($listen -and $listen.internal_url) { $centerUrl = [string]$listen.internal_url }
elseif ($env:ECMP_CENTER_TLS_CERT -or ($listen -and $listen.tls_cert)) { $centerUrl = 'https://localhost:8010' }
$uiScheme = if ($listen -and $listen.ui_tls_cert) { 'https' } else { 'http' }
function Ui-Url([int]$port) { return "${uiScheme}://localhost:$port/center" }
function Ready($url) { try { return (Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200 } catch { return $false } }
function Wait-Ready($url, [int]$seconds, $process) {
    $end = [DateTime]::UtcNow.AddSeconds($seconds)
    while ([DateTime]::UtcNow -lt $end) {
        if (Ready $url) { return $true }
        if ($process) { $process.Refresh(); if ($process.HasExited) { return $false } }
        Start-Sleep -Seconds 1
    }
    return $false
}
function Port-Busy([int]$port) {
    $tcp = [Net.Sockets.TcpClient]::new()
    try { $tcp.Connect('127.0.0.1', $port); return $true } catch { return $false } finally { $tcp.Dispose() }
}

# ---------- 安全停止：端口监听进程 + 命令行核验 ----------
# venv 的 python.exe 在 Windows 上会再启动基础解释器，所以向上查找祖先进程确认归属本项目。
function Ancestors($proc) {
    $chain = @()
    $current = $proc
    for ($i = 0; $i -lt 4 -and $current; $i++) {
        $chain += $current
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($current.ParentProcessId)" -ErrorAction SilentlyContinue
    }
    return $chain
}
function Stop-VerifiedListener([int]$port, [string]$pattern, [string]$wrapper, [string]$name) {
    $owners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
    if (-not $owners) { Info "$name 未在端口 $port 运行，跳过停止。"; return }
    foreach ($owner in $owners) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $owner"
        if (-not $proc) { continue }
        $chain = Ancestors $proc
        $line = [string]$proc.CommandLine
        $inProject = @($chain | Where-Object { ([string]$_.CommandLine) -like "*$root*" -or ([string]$_.ExecutablePath) -like "$root*" }).Count -gt 0
        if ($line -notmatch $pattern -or -not $inProject) {
            throw "端口 $port 的进程 PID $owner 不能确认是本项目的$name（$line），已停止执行，请人工核对。"
        }
        # 隐藏启动包装（powershell -File scripts\start-center*.ps1）一并结束；交互式终端本身不会匹配。
        $target = $owner
        $wrap = $chain | Where-Object { ([string]$_.CommandLine) -match $wrapper } | Select-Object -First 1
        if ($wrap) { $target = $wrap.ProcessId }
        Info "停止$name：PID $target（监听 PID $owner）"
        Native { & taskkill.exe /PID $target /T /F | Out-Null }
    }
    $end = [DateTime]::UtcNow.AddSeconds(20)
    while ((Port-Busy $port) -and [DateTime]::UtcNow -lt $end) { Start-Sleep -Milliseconds 500 }
    if (Port-Busy $port) { throw "$name 停止后端口 $port 仍被占用" }
}

function Start-Hidden([string]$name, [string[]]$scriptArgs) {
    $err = Join-Path $logs "$name-$stamp.err.log"
    $shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $p = Start-Process $shell -ArgumentList (@('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File') + $scriptArgs) `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logs "$name-$stamp.out.log") -RedirectStandardError $err
    return $p
}

$lock = Join-Path $root '.data\release-and-restart.lock'
$lockStream = $null
$exitCode = 0
try {
    try { $lockStream = [IO.File]::Open($lock, 'OpenOrCreate', 'ReadWrite', 'None') }
    catch { throw '另一个发布/重启脚本正在运行，请等待其结束。' }

    if (-not (Test-Path -LiteralPath $python)) { throw "缺少 $python" }

    # ---------- 1. 判断改动范围（只读 Git，不暂存不提交） ----------
    $changed = @()
    $gitKnown = $true
    try { $changed = @(Git-Lines @('status', '--porcelain=v1', '--untracked-files=all', '--', 'backend', 'config', 'app', 'lib', 'public', 'server.cjs', 'next.config.mjs', 'package.json', 'package-lock.json') | ForEach-Object { $_.Substring(3).Trim('"') }) }
    catch { $gitKnown = $false; Warn '无法读取 Git 状态，按“全部有改动”处理。' }
    $backendChanged = (-not $gitKnown) -or @($changed | Where-Object { ($_ -like 'backend/*' -and $_ -notlike 'backend/tests/*') -or $_ -like 'config/*' }).Count -gt 0
    $webChanged = (-not $gitKnown) -or @($changed | Where-Object { $_ -match '^(app|lib|public)/' -or $_ -in @('server.cjs', 'next.config.mjs', 'package.json', 'package-lock.json') }).Count -gt 0
    $doCenter = ($Center -eq 'always') -or ($Center -eq 'auto' -and $backendChanged)
    $doWeb = ($Web -eq 'always') -or ($Web -eq 'auto' -and $webChanged)

    Step '执行计划（不提交 Git）'
    if ($SkipRelease) { Info '跳过测试与发布（-SkipRelease），只执行部署步骤。' }
    elseif ($changed) { Info ("相对上次提交的未提交改动：`n  " + ($changed -join "`n  ")) }
    Info ("重启中心：" + $(if ($doCenter) { '是' } else { '否' }))
    Info ("切换网页：" + $(if ($doWeb) { '是（构建新目录 → 预览端口验证 → 切换，失败回退）' } else { '否' }))
    Info ("等待本机采集端自动升级：" + $(if ($SkipCollectorWait -or $SkipRelease) { '否' } else { "是（最长 $CollectorTimeoutMinutes 分钟）" }))
    if ($DryRun) { Info '预览结束：未测试、未发布、未重启。'; return }

    # ---------- 2. 测试 -> 构建 -> 签名发布 ----------
    $node = Join-Path $root '.runtime\node.exe'
    if (-not (Test-Path -LiteralPath $node)) { $node = (Get-Command node -ErrorAction Stop).Source }
    $latestFile = Join-Path $root ('.data\center\releases\' + $(if ($NodeId) { "latest-$NodeId.json" } else { 'latest.json' }))
    $before = Read-Json $latestFile
    $previousVersion = if ($before) { [string]$before.release.version } else { '' }
    $newDist = $WebDistDir
    function Code-Fingerprint {
        $files = foreach ($folder in @('backend', 'config')) {
            Get-ChildItem -LiteralPath (Join-Path $root $folder) -Recurse -File | Where-Object { $_.FullName -notmatch '\\__pycache__\\' -and $_.Extension -ne '.pyc' }
        }
        return (($files | Sort-Object FullName | ForEach-Object { $_.FullName + '=' + (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }) -join "`n")
    }
    if (-not $SkipRelease) {
        $baseline = Code-Fingerprint
        Step '后端发布门禁（隔离临时数据目录）'
        $testData = Join-Path ([IO.Path]::GetTempPath()) ('ecmp-release-tests-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $testData | Out-Null
        $savedData = $env:PDD_DATA_DIR
        $env:PDD_DATA_DIR = $testData
        try { Native { & $python -m unittest discover -s backend/tests 2>&1 | ForEach-Object { if ($_ -is [Management.Automation.ErrorRecord]) { [string]$_.TargetObject } else { "$_" } } | Write-Host } }
        finally { $env:PDD_DATA_DIR = $savedData; Remove-Item -LiteralPath $testData -Recurse -Force -ErrorAction SilentlyContinue }
        if ($LASTEXITCODE -ne 0) { throw '后端测试未通过，已停止：未发布、未重启。' }

        Step '前端单元测试'
        Native { & $node scripts/test-web.cjs 2>&1 | ForEach-Object { if ($_ -is [Management.Automation.ErrorRecord]) { [string]$_.TargetObject } else { "$_" } } | Write-Host }
        if ($LASTEXITCODE -ne 0) { throw '前端测试未通过，已停止：未发布、未重启。' }

        if ($doWeb -and -not $WebDistDir) {
            Step '构建网页（新的独立目录，不覆盖正在运行的构建）'
            $newDist = '.next-release-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
            $savedDist = $env:PDD_NEXT_DIST_DIR
            $env:PDD_NEXT_DIST_DIR = $newDist
            try { Native { & $node node_modules/next/dist/bin/next build --webpack 2>&1 | ForEach-Object { if ($_ -is [Management.Automation.ErrorRecord]) { [string]$_.TargetObject } else { "$_" } } | Write-Host } }
            finally { $env:PDD_NEXT_DIST_DIR = $savedDist }
            if ($LASTEXITCODE -ne 0) { throw '网页构建失败，已停止：未发布、未重启。' }
            Info "网页构建完成：$newDist"
        }

        if ((Code-Fingerprint) -ne $baseline) { throw '测试期间 backend/config 代码发生变化，已停止，请重新运行。' }
        Step '签名发布采集端代码（不提交 Git，使用 --allow-dirty，复用刚通过的测试）'
        $publishArgs = @('scripts\publish-collector-update.py', '--allow-dirty', '--skip-tests')
        if ($NodeId) { $publishArgs += @('--node-id', $NodeId) }
        Native { & $python @publishArgs 2>&1 | ForEach-Object { if ($_ -is [Management.Automation.ErrorRecord]) { [string]$_.TargetObject } else { "$_" } } | Write-Host }
        if ($LASTEXITCODE -ne 0) { throw '签名发布失败，已停止，服务未重启。' }
    }
    $after = Read-Json $latestFile
    $publishedVersion = if ($after) { [string]$after.release.version } else { '' }
    if ($publishedVersion -and $publishedVersion -ne $previousVersion) { Info "已发布采集端版本：$publishedVersion" }
    elseif (-not $SkipRelease) { Info '采集端打包内容无变化，沿用原版本。' }

    # ---------- 2b. 发布记录（不提交 Git，但保留可回退的代码快照） ----------
    if (-not $SkipRelease) {
        try {
            $recordDir = Join-Path $root '.data\releases'
            New-Item -ItemType Directory -Path $recordDir -Force | Out-Null
            $label = if ($publishedVersion) { $publishedVersion } else { 'web-' + $stamp }
            $patch = Join-Path $recordDir "$label.patch"
            $safe = 'safe.directory=' + ($root -replace '\\', '/')
            $scope = @('backend', 'config', 'app', 'lib', 'public', 'scripts', 'server.cjs', 'next.config.mjs', 'package.json', 'package-lock.json')
            # 相对 HEAD 的完整差异（含新增文件，使用临时索引，不改动工作区或真实暂存区）。
            $tempIndex = Join-Path $recordDir "index-$stamp.tmp"
            $savedIndex = $env:GIT_INDEX_FILE
            try {
                $env:GIT_INDEX_FILE = $tempIndex
                Native { & git -c $safe read-tree HEAD 2>$null | Out-Null }
                Native { & git -c $safe add -A -- @scope 2>$null | Out-Null }
                # git writes the patch itself so line endings and binary content stay exact.
                Native { & git -c $safe diff --cached --binary HEAD "--output=$patch" -- @scope 2>$null | Out-Null }
            }
            finally {
                $env:GIT_INDEX_FILE = $savedIndex
                Remove-Item -LiteralPath $tempIndex -Force -ErrorAction SilentlyContinue
            }
            $head = (Native { & git -c $safe rev-parse HEAD 2>$null }) | Select-Object -First 1
            $entry = [ordered]@{
                time = (Get-Date).ToString('o'); version = $publishedVersion; previous = $previousVersion
                git_head = [string]$head; web_build = $newDist; patch = ".data/releases/$label.patch"
                files = @($changed)
            } | ConvertTo-Json -Compress -Depth 4
            [IO.File]::AppendAllText((Join-Path $recordDir 'history.jsonl'), $entry + "`n", $utf8)
            Info "发布记录：.data\releases\$label.patch（基于提交 $head；回退代码可 git checkout -- . 后 git apply 旧记录）"
        }
        catch { Warn ('发布记录未写入（不影响发布）：' + $_.Exception.Message) }
    }

    # ---------- 3. 重启中心 ----------
    if ($doCenter) {
        Step '重启中心服务'
        Stop-VerifiedListener 8010 'backend\.center\.server' 'start-center\.ps1' '中心服务'
        $p = Start-Hidden 'center' @('scripts\start-center.ps1')
        if (-not (Wait-Ready "$centerUrl/health" 90 $p)) { throw "中心服务未能启动，请查看 $logs\center-$stamp.err.log" }
        Info "中心已就绪：$centerUrl/health"
    }

    # ---------- 4. 切换网页构建（预览验证 -> 切换 -> 失败回退） ----------
    if ($doWeb) {
        Step '切换网页后台构建'
        if (-not $newDist) { throw '没有可用的新构建目录；请用 -WebDistDir 指定，或不要同时使用 -SkipRelease 与 -Web always。' }
        $newDist = Get-UiBuildDirectory -ProjectRoot $root -Override $newDist
        if (-not (Test-Path -LiteralPath (Join-Path $newDist 'BUILD_ID'))) { throw "构建目录 $newDist 缺少 BUILD_ID" }
        $oldDist = Get-UiBuildDirectory -ProjectRoot $root
        $previewPort = 3212
        if (Port-Busy $previewPort) { throw "预览端口 $previewPort 已被占用，请先释放" }
        $preview = Start-Hidden 'admin-preview' @('scripts\start-center-ui.ps1', '-Port', "$previewPort", '-ListenAddress', '127.0.0.1', '-DistDir', $newDist)
        try {
            if (-not (Wait-Ready (Ui-Url $previewPort) 90 $preview)) { throw "新构建 $newDist 预览验证失败，未切换，请查看 $logs\admin-preview-$stamp.err.log" }
            Info "预览验证通过：$newDist"
        }
        finally { Native { & taskkill.exe /PID $preview.Id /T /F 2>$null | Out-Null } }

        # 本机覆盖文件优先于 config\ui-build.json，且不产生 Git 变更。
        $localConfig = Join-Path $root '.data\center\ui-build.local.json'
        $localBackup = if (Test-Path -LiteralPath $localConfig) { Get-Content -LiteralPath $localConfig -Raw -Encoding UTF8 } else { $null }
        function Set-Dist($dir) {
            $json = @{ dist_dir = $dir } | ConvertTo-Json
            [IO.File]::WriteAllText($localConfig, $json + "`n", [Text.UTF8Encoding]::new($false))
        }
        Stop-VerifiedListener 3211 'server\.cjs' 'start-center-ui\.ps1' '网页后台'
        Set-Dist $newDist
        $web = Start-Hidden 'admin' @('scripts\start-center-ui.ps1', '-ListenAddress', '0.0.0.0')
        if (Wait-Ready (Ui-Url 3211) 90 $web) {
            Info "网页后台已切换：$oldDist → $newDist"
        }
        else {
            Warn "新构建启动失败，回退到 $oldDist"
            Native { & taskkill.exe /PID $web.Id /T /F 2>$null | Out-Null }
            if ($null -ne $localBackup) { [IO.File]::WriteAllText($localConfig, $localBackup, [Text.UTF8Encoding]::new($false)) }
            else { Remove-Item -LiteralPath $localConfig -Force }
            $web = Start-Hidden 'admin-rollback' @('scripts\start-center-ui.ps1', '-ListenAddress', '0.0.0.0')
            if (-not (Wait-Ready (Ui-Url 3211) 90 $web)) { throw "回退后网页仍未启动，请查看 $logs" }
            throw "网页切换失败，已回退到 $oldDist；请查看 $logs\admin-$stamp.err.log"
        }
    }

    # ---------- 5. 等待本机采集端自动升级 ----------
    $waitCollector = -not $SkipCollectorWait -and $publishedVersion -and $publishedVersion -ne $previousVersion
    if ($waitCollector -and $NodeId) {
        $local = Read-Json (Join-Path $root '.data\collector-connection.local.json')
        if (-not $local -or [string]$local.id -ne $NodeId) { Info '灰度发布目标不是本机节点，不等待本机升级。'; $waitCollector = $false }
    }
    if ($waitCollector) {
        Step "等待本机采集端自动升级到 $publishedVersion"
        if (-not (Ready 'http://127.0.0.1:8001/health')) {
            Warn '本机采集服务未运行（127.0.0.1:8001 无响应），跳过等待；采集端启动后会自动检查更新。'
        }
        else {
            $updates = Join-Path $root '.data\updates'
            $end = [DateTime]::UtcNow.AddMinutes($CollectorTimeoutMinutes)
            $last = ''
            $done = $false
            while ([DateTime]::UtcNow -lt $end) {
                $status = Read-Json (Join-Path $updates 'status.json')
                $installed = Read-Json (Join-Path $root 'collector-version.json')
                $text = if ($status -and $status.message) { [string]$status.message } else { '等待采集端检查更新' }
                if ($text -ne $last) { Info ("[{0}] {1}" -f (Get-Date -Format HH:mm:ss), $text); $last = $text }
                if ($text -match '回退') { throw "采集端新版试运行失败并已自动回退：$text" }
                if ($installed -and [string]$installed.version -eq $publishedVersion -and $status -and
                    [string]$status.version -eq $publishedVersion -and $text -match '自动升级成功' -and
                    -not (Test-Path -LiteralPath (Join-Path $updates 'journal.json'))) { $done = $true; break }
                Start-Sleep -Seconds 10
            }
            if ($done) { Info "本机采集端自动升级成功：$publishedVersion（试运行已通过）" }
            else { Warn "等待超过 $CollectorTimeoutMinutes 分钟仍未确认升级成功，最后状态：$last。请在采集控制台查看。"; $exitCode = 3 }
        }
    }

    Step '完成'
    if (Ready "$centerUrl/health") { Info "中心：正常（$centerUrl）" } else { Warn '中心健康检查未通过' ; $exitCode = [Math]::Max($exitCode, 2) }
    if (Ready (Ui-Url 3211)) { Info ("网页：正常（{0}，构建 {1}）" -f (Ui-Url 3211), (Get-UiBuildDirectory -ProjectRoot $root)) } else { Warn '网页健康检查未通过'; $exitCode = [Math]::Max($exitCode, 2) }
    Info "日志：$transcript"
}
catch {
    Write-Host ''
    Write-Host ('失败：' + $_.Exception.Message) -ForegroundColor Red
    Write-Host "日志：$transcript"
    $exitCode = 1
}
finally {
    if ($lockStream) { $lockStream.Dispose(); Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue }
    Stop-Transcript | Out-Null
}
exit $exitCode
