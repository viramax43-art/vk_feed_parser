```bat
@echo off
rem Build VK Feed Parser.exe. Run from the project folder.
rem NOTE: this file must stay ASCII-only. cmd.exe reads .bat files in the
rem console code page, so Cyrillic text here breaks command parsing.
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python build_exe.py
if errorlevel 1 goto failed
echo.
echo Done. Program: dist\VK Feed Parser\VK Feed Parser.exe
pause
exit /b 0
:failed
echo.
echo Build failed. See the error above.
pause
exit /b 1

```