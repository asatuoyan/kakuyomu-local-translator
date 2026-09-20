@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating Python environment...
  py -3.11 -m venv .venv
  if errorlevel 1 goto :error
)

echo [2/3] Installing dependencies...
call .venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :error
call .venv\Scripts\python.exe -m playwright install chromium
if errorlevel 1 goto :error

if not exist "config.json" copy /Y "config.example.json" "config.json" >nul

echo [3/3] Starting translator...
call .venv\Scripts\python.exe main.py
if errorlevel 1 goto :error
goto :end

:error
echo.
echo Failed. Please copy the error message and ask for help.
pause
exit /b 1

:end
pause

