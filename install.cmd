@echo off
setlocal
cd /d "%~dp0"
python sjtuclaw_launcher.py install
if errorlevel 1 (
  echo.
  echo Installation did not complete. See the messages above.
  pause
)
