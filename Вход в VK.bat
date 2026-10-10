@echo off
cd /d "%~dp0"
echo ============================================
echo   VK login (once per account; solve captcha yourself)
echo ============================================
echo.
where python >nul 2>nul
if %errorlevel%==0 (
  python src\vk_auth.py --login --timeout 1800
  echo.
  echo Session status:
  python src\vk_auth.py --status
  pause
  exit /b 0
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 src\vk_auth.py --login --timeout 1800
  echo.
  echo Session status:
  py -3 src\vk_auth.py --status
  pause
  exit /b 0
)
echo Python not found. Install from https://www.python.org/downloads/
pause
exit /b 1
