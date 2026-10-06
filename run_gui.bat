@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_environment.ps1" %*
if errorlevel 1 goto :error
start "" ".venv\Scripts\pythonw.exe" gui.py
if errorlevel 1 goto :error
exit /b 0
:error
echo Failed to launch GUI. Please check the error message.
pause
exit /b 1
