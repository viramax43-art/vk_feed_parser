@echo off
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  start "" /min python app.py --gui
  if errorlevel 1 (
    echo Failed to start GUI. Showing errors...
    python app.py --gui
    pause
  )
  exit /b 0
)
where py >nul 2>nul
if %errorlevel%==0 (
  start "" /min py -3 app.py --gui
  if errorlevel 1 (
    echo Failed to start GUI. Showing errors...
    py -3 app.py --gui
    pause
  )
  exit /b 0
)
echo Python not found. Install from https://www.python.org/downloads/
pause
exit /b 1
