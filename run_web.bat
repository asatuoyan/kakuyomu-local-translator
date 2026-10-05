@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_environment.ps1" %*
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -X utf8 web_app.py
if errorlevel 1 goto :error
exit /b 0
:error
echo Failed to launch local web app. Please check the error message.
pause
exit /b 1
