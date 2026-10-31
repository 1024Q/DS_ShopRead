param([int]$Port=8010,[string]$TlsCertFile='',[string]$TlsKeyFile='')
$ErrorActionPreference='Stop'
# Run on the center host with an elevated PowerShell. Only the local subnet may connect.
if ([bool]$TlsCertFile -ne [bool]$TlsKeyFile) { throw 'TLS 需要同时提供证书文件和私钥文件' }
foreach ($file in @($TlsCertFile,$TlsKeyFile) | Where-Object { $_ }) { if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "TLS 文件不存在：$file" } }
$name='ECMP-Center-LAN-'+$Port
$existing=Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
if ($existing) {
    $existing | Set-NetFirewallRule -Enabled True -Direction Inbound -Action Allow -Profile Any
    $existing | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress LocalSubnet
    $existing | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $Port
} else {
    New-NetFirewallRule -Name $name -DisplayName "ECMP Center LAN $Port" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress LocalSubnet -Profile Any | Out-Null
}
$configDirectory=Join-Path (Split-Path $PSScriptRoot -Parent) '.data/center'
New-Item -ItemType Directory -Path $configDirectory -Force | Out-Null
$listen=[ordered]@{address='0.0.0.0'}
if ($TlsCertFile) { $listen.tls_cert=(Resolve-Path -LiteralPath $TlsCertFile).Path; $listen.tls_key=(Resolve-Path -LiteralPath $TlsKeyFile).Path }
[IO.File]::WriteAllText((Join-Path $configDirectory 'listen.local.json'),($listen | ConvertTo-Json -Compress))
Write-Output "LAN firewall rule enabled for TCP $Port (LocalSubnet only)."
if ($TlsCertFile) {
    Write-Output "HTTPS enabled. Collectors connect with https://<center-ip>:$Port (set ECMP_CENTER_CA_FILE on collectors for a private CA)."
} else {
    Write-Warning "No TLS certificate configured: LAN traffic (node keys, admin token, business data) will be plain HTTP. Re-run with -TlsCertFile/-TlsKeyFile to enable HTTPS."
}
Write-Output "Start center with: .\scripts\start-center.ps1 -Port $Port"
