param([int]$Port=3211,[string]$DistDir='',[switch]$LocalHttp,[ValidateSet('127.0.0.1','0.0.0.0')][string]$ListenAddress='0.0.0.0',[string]$TlsCertFile='',[string]$TlsKeyFile='')
$ErrorActionPreference='Stop'
Set-Location (Join-Path $PSScriptRoot '..')
. (Join-Path $PSScriptRoot 'ui-config.ps1')
$DistDir = Get-UiBuildDirectory -ProjectRoot (Get-Location).Path -Override $DistDir
$listen = $null
if (Test-Path -LiteralPath '.data/center/listen.local.json') { $listen = Get-Content -LiteralPath '.data/center/listen.local.json' -Raw | ConvertFrom-Json }
if (-not $TlsCertFile -and $listen) { $TlsCertFile = [string]$listen.ui_tls_cert }
if (-not $TlsKeyFile -and $listen) { $TlsKeyFile = [string]$listen.ui_tls_key }
if ([bool]$TlsCertFile -ne [bool]$TlsKeyFile) { throw 'UI TLS requires both certificate and key.' }
if ($LocalHttp -and $TlsCertFile) { throw 'LocalHttp cannot be combined with UI TLS.' }
foreach ($file in @($TlsCertFile,$TlsKeyFile)) { if ($file -and -not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing TLS file: $file" } }
$env:ECMP_UI_TLS_CERT = $TlsCertFile
$env:ECMP_UI_TLS_KEY = $TlsKeyFile
if (-not (Test-Path -LiteralPath (Join-Path $DistDir 'BUILD_ID'))) { throw "未找到构建目录 $DistDir，请先设置 PDD_NEXT_DIST_DIR 并执行 npm run build" }
$env:ECMP_DEPLOYMENT_MODE='center'
$env:ECMP_CENTER_INTERNAL_URL='http://127.0.0.1:8010'
if ($listen -and $listen.internal_url) { $env:ECMP_CENTER_INTERNAL_URL = [string]$listen.internal_url }
elseif ($env:ECMP_CENTER_TLS_CERT -or ($listen -and $listen.tls_cert)) { $env:ECMP_CENTER_INTERNAL_URL = 'https://localhost:8010' }
if ($listen -and $listen.ca_file) { $env:NODE_EXTRA_CA_CERTS = [string]$listen.ca_file }
$env:PDD_UI_HOST=$ListenAddress
$env:PDD_UI_PORT=[string]$Port
$env:PDD_NEXT_DIST_DIR=$DistDir
$env:ECMP_COOKIE_SECURE=if ($TlsCertFile) {'true'} else {'false'}
if (-not $TlsCertFile -and $ListenAddress -eq '0.0.0.0') { Write-Warning 'Web UI is using unencrypted LAN HTTP. Configure ui_tls_cert/ui_tls_key before switching to HTTPS.' }
$node=Join-Path (Get-Location) '.runtime\node.exe'
if (-not (Test-Path -LiteralPath $node)) {$node=(Get-Command node -ErrorAction Stop).Source}
& $node server.cjs
