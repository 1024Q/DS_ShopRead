@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" "scripts\release-update.py" %*
exit /b %errorlevel%
