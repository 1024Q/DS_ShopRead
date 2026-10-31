param([int]$Port=8010,[string]$ListenAddress='127.0.0.1',[string]$TlsCertFile='',[string]$TlsKeyFile='')
$ErrorActionPreference='Stop'
foreach ($workerValue in @($env:WEB_CONCURRENCY,$env:UVICORN_WORKERS)) { if ($workerValue -and [int]$workerValue -ne 1) { throw '中心隧道仅支持单 worker' } }
Set-Location (Join-Path $PSScriptRoot '..')
$listen=$null
if (Test-Path -LiteralPath '.data/center/listen.local.json') { $listen=Get-Content -LiteralPath '.data/center/listen.local.json' -Raw | ConvertFrom-Json }
if (-not $PSBoundParameters.ContainsKey('ListenAddress') -and $listen) {
    $ListenAddress=[string]$listen.address
    if ($ListenAddress -notin @('127.0.0.1','0.0.0.0')) { throw '中心监听配置无效' }
}
# TLS: explicit parameters, then environment, then the LAN listen configuration.
if (-not $TlsCertFile) { $TlsCertFile=[string]$env:ECMP_CENTER_TLS_CERT }
if (-not $TlsKeyFile) { $TlsKeyFile=[string]$env:ECMP_CENTER_TLS_KEY }
if (-not $TlsCertFile -and $listen -and $listen.tls_cert) { $TlsCertFile=[string]$listen.tls_cert }
if (-not $TlsKeyFile -and $listen -and $listen.tls_key) { $TlsKeyFile=[string]$listen.tls_key }
if ([bool]$TlsCertFile -ne [bool]$TlsKeyFile) { throw 'TLS 需要同时提供证书文件和私钥文件' }
$tls=@()
if ($TlsCertFile) {
    foreach ($file in @($TlsCertFile,$TlsKeyFile)) { if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "TLS 文件不存在：$file" } }
    $tls=@('--ssl-certfile',$TlsCertFile,'--ssl-keyfile',$TlsKeyFile)
}
$configFile=Join-Path (Get-Location) '.data\center\center.local.json'
if (Test-Path -LiteralPath $configFile) {
    $config=Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
    foreach ($name in @('ECMP_DATABASE_URL','ECMP_CENTER_ADMIN_TOKEN')) {
        if (-not [Environment]::GetEnvironmentVariable($name)) {
            [Environment]::SetEnvironmentVariable($name,[string]$config.$name,'Process')
        }
    }
}
if (-not $env:ECMP_DATABASE_URL -or -not $env:ECMP_CENTER_ADMIN_TOKEN) { throw '请先配置 ECMP_DATABASE_URL 和 ECMP_CENTER_ADMIN_TOKEN' }
if ($ListenAddress -notin @('127.0.0.1','::1','localhost') -and -not $tls) {
    Write-Warning '================================================================'
    Write-Warning "中心正以明文 HTTP 监听 $ListenAddress`:$Port。局域网内的节点密钥、管理令牌和业务数据未加密。"
    Write-Warning '建议使用 -TlsCertFile/-TlsKeyFile（或 ECMP_CENTER_TLS_CERT/ECMP_CENTER_TLS_KEY）启用 HTTPS；'
    Write-Warning '采集节点改用 https:// 地址，自签证书可通过 ECMP_CENTER_CA_FILE 指定信任的 CA。'
    Write-Warning '================================================================'
}
& .\.venv\Scripts\python.exe -m uvicorn backend.center.server:create_app --factory --host $ListenAddress --port $Port --ws-max-size 50331648 @tls
