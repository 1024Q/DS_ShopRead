param([switch]$CheckOnly)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $root
. (Join-Path $PSScriptRoot 'ui-config.ps1')
$distDir = Get-UiBuildDirectory -ProjectRoot $root
foreach ($file in @('.venv\Scripts\python.exe','scripts\start-center.ps1','scripts\start-center-ui.ps1',(Join-Path $distDir 'BUILD_ID'))) {
 if (-not (Test-Path -LiteralPath $file)) { throw "Required file missing: $file" }
}
if ($CheckOnly) { Write-Host 'Check passed. No services started.'; exit 0 }
$logs=Join-Path $root '.data\service-logs'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
function Ready($url) { try { return (Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } catch { return $false } }
function Launch($name,$port,$url,$scriptArgs) {
 if (Ready $url) { Write-Host "$name already running."; return }
 $tcp=[Net.Sockets.TcpClient]::new()
 try { $tcp.Connect('127.0.0.1',$port); $busy=$true } catch { $busy=$false } finally { $tcp.Dispose() }
 if ($busy) { throw "Port $port is occupied. Check existing process." }
 $stamp=Get-Date -Format yyyyMMdd-HHmmss-fff
 $err=Join-Path $logs "$name-$stamp.err.log"
 $shell=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
 $p=Start-Process $shell -ArgumentList (@('-NoProfile','-ExecutionPolicy','Bypass','-File')+$scriptArgs) -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs "$name-$stamp.out.log") -RedirectStandardError $err -PassThru
 $end=[DateTime]::UtcNow.AddSeconds(90)
 while ([DateTime]::UtcNow -lt $end) {
  if (Ready $url) { Write-Host "$name started."; return }
  $p.Refresh()
  if ($p.HasExited) { throw "$name exited. See $err" }
  Start-Sleep -Seconds 1
 }
 throw "$name is not ready. Check $logs before retrying."
}
$listen = $null
if (Test-Path -LiteralPath '.data/center/listen.local.json') { $listen = Get-Content -LiteralPath '.data/center/listen.local.json' -Raw | ConvertFrom-Json }
$centerUrl = 'http://127.0.0.1:8010'
if ($listen -and $listen.internal_url) { $centerUrl = [string]$listen.internal_url }
elseif ($env:ECMP_CENTER_TLS_CERT -or ($listen -and $listen.tls_cert)) { $centerUrl = 'https://localhost:8010' }
$uiUrl = if ($listen -and $listen.ui_tls_cert) { 'https://localhost:3211/center' } else { 'http://localhost:3211/center' }
Launch 'center' 8010 "$centerUrl/health" @('scripts\start-center.ps1')
Launch 'admin' 3211 $uiUrl @('scripts\start-center-ui.ps1','-ListenAddress','0.0.0.0')
Write-Host "Ready: $uiUrl. Logs: $logs"
Start-Process $uiUrl
