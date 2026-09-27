@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_environment.ps1" %*
if errorlevel 1 goto :error
call .venv\Scripts\python.exe main.py
if errorlevel 1 goto :error
pause
exit /b 0
:error
echo Failed. Please copy the error message and ask for help.
pause
exit /b 1