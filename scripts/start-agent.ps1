param([switch]$LocalCenter)
$ErrorActionPreference='Stop'
Set-Location (Join-Path $PSScriptRoot '..')
if ($LocalCenter) {
    $config=Get-Content -LiteralPath .data/center/local-node.local.json -Raw | ConvertFrom-Json
    $env:ECMP_CENTER_URL='http://127.0.0.1:8010'
    $env:ECMP_NODE_ID=$config.id
    $env:ECMP_NODE_TOKEN=$config.token
    $env:PDD_DATA_DIR=Join-Path (Get-Location) '.data'
}
foreach ($name in @('ECMP_CENTER_URL','ECMP_NODE_ID','ECMP_NODE_TOKEN')) {
    if (-not [Environment]::GetEnvironmentVariable($name)) { throw "请先配置 $name" }
}
& .\.venv\Scripts\python.exe -m backend.center.agent
