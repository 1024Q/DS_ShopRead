param([int]$DatabasePort=5432)
$ErrorActionPreference='Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$directory=Join-Path (Get-Location) '.data\center'
if (-not (Test-Path -LiteralPath $directory)) {
    New-Item -ItemType Directory -Path $directory | Out-Null
}
# Restrict this new configuration directory before generating any credentials.
$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $directory /inheritance:r /grant:r "*${sid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw '无法限制配置目录权限，已停止初始化' }
& .\.venv\Scripts\python.exe -m backend.center.setup --port $DatabasePort --directory $directory
if ($LASTEXITCODE -ne 0) { throw '中心数据库未完成初始化，请检查上面的提示' }
