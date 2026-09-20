@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating Python environment...
  py -3.11 -m venv .venv
  if errorlevel 1 goto :error
)

echo [1/2] Checking dependencies...
call .venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :error
call .venv\Scripts\python.exe -m playwright install chromium
if errorlevel 1 goto :error

if not exist "config.json" copy /Y "config.example.json" "config.json" >nul

echo [2/2] Launching GUI...
start "" ".venv\Scripts\pythonw.exe" gui.py
if errorlevel 1 (
  call .venv\Scripts\python.exe gui.py
)
goto :end

:error
echo.
echo Failed to launch GUI. Please check error message.
pause
exit /b 1

:end
