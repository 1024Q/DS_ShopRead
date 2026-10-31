@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\release-and-restart.ps1" %*
set code=%errorlevel%
echo.
if %code%==0 (echo Done.) else (echo Finished with exit code %code%. See the log path above.)
pause
exit /b %code%
