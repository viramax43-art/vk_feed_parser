@echo off
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  python src\run_tags.py %*
  pause
  exit /b %errorlevel%
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 src\run_tags.py %*
  pause
  exit /b %errorlevel%
)
echo Python not found. Install from https://www.python.org/downloads/
pause
exit /b 1
