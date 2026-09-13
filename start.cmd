@echo off
setlocal
cd /d "%~dp0"
python sjtuclaw_launcher.py start --pet
if errorlevel 1 (
  echo.
  echo SJTUClaw failed to start. See the messages above.
  pause
)
