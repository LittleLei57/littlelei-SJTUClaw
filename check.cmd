@echo off
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
python scripts\check.py %*
set "exit_code=%ERRORLEVEL%"
echo.
if not "%exit_code%"=="0" (
  echo Project self-check failed. Review the diagnostics above and retry.
) else (
  echo Project self-check passed.
)
exit /b %exit_code%
