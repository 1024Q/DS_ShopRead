@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-backend-all.ps1"
if errorlevel 1 (
    echo.
    echo Startup failed. Please check the error above.
    pause
    exit /b 1
)
echo.
echo Center and admin are running in the background. You can close this window.
pause
