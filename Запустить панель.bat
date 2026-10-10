@echo off
REM ASCII-only: UTF-8 Cyrillic without BOM breaks cmd.exe parsing.
cd /d "%~dp0"
title VK Feed Parser - browser panel

where python >nul 2>nul
if %errorlevel%==0 (
  python start_panel.py %*
  goto :after
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 start_panel.py %*
  goto :after
)

echo.
echo Python not found. Install from https://www.python.org/downloads/
echo and enable "Add python.exe to PATH" during setup.
echo.
pause
exit /b 1

:after
echo.
echo Panel stopped. You can close this window.
pause
