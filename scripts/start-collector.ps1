# Collector-only machine: no Next.js and no central database credentials required.
$ErrorActionPreference='Stop'
Set-Location (Join-Path $PSScriptRoot '..')
if (Get-NetTCPConnection -State Listen -LocalPort 8001 -ErrorAction SilentlyContinue) { throw '8001 已有服务运行；请勿重复启动采集服务' }
& .\.venv\Scripts\python.exe -m uvicorn backend.collector_server:app --host 127.0.0.1 --port 8001
