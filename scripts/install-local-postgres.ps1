# Run on the chosen center Windows machine with administrator privileges.
$ErrorActionPreference='Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$installer=Join-Path (Get-Location) 'tmp\postgresql-17.11-5-windows-x64.exe'
if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -ne '612C7F5400A003AAFFA0372CB273EED52278F93C680AB911EB9CB2229E329072') { throw '安装包校验失败' }
if ((Get-AuthenticodeSignature -LiteralPath $installer).Status -ne 'Valid') { throw '安装包签名验证失败' }
if (Get-Service -Name 'ecmp-postgresql-17' -ErrorAction SilentlyContinue) { throw 'ECMP PostgreSQL 服务已存在，停止重复安装' }
if (Test-Path 'C:\Program Files\PostgreSQL\17') { throw 'PostgreSQL 17 安装目录已存在，停止以免影响现有数据库' }
if (Get-NetTCPConnection -State Listen -LocalPort 5432 -ErrorAction SilentlyContinue) { throw '5432 端口已被占用' }
$directory=Join-Path (Get-Location) '.data\center'
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $directory /inheritance:r /grant:r "*${sid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw '无法限制配置目录权限' }
$passwordFile=Join-Path $directory 'postgres-admin.local.json'
if (-not (Test-Path -LiteralPath $passwordFile)) {
    $bytes=New-Object byte[] 48
    $rng=[System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {$rng.GetBytes($bytes)} finally {$rng.Dispose()}
    $password=[Convert]::ToBase64String($bytes)
    @{password=$password} | ConvertTo-Json | Set-Content -LiteralPath $passwordFile -Encoding UTF8
} else { $password=(Get-Content -LiteralPath $passwordFile -Raw | ConvertFrom-Json).password }
$optionFile=Join-Path $directory 'installer-options.local.txt'
@("mode=unattended","unattendedmodeui=none","enable-components=server,commandlinetools","disable-components=pgAdmin,stackbuilder","serverport=5432","servicename=ecmp-postgresql-17","superpassword=$password") | Set-Content -LiteralPath $optionFile -Encoding ASCII
try {
    $process=Start-Process -FilePath $installer -ArgumentList @('--optionfile',('"'+$optionFile+'"')) -WindowStyle Hidden -PassThru -Wait
    if ($process.ExitCode -ne 0) { throw "PostgreSQL 安装未成功，退出码 $($process.ExitCode)" }
} finally { Remove-Item -LiteralPath $optionFile -Force -ErrorAction SilentlyContinue }
Get-Service -Name 'ecmp-postgresql-17' | Select-Object Name,Status
Write-Output '管理员密码已保存在受限的 .data/center/postgres-admin.local.json，未输出到日志。'
