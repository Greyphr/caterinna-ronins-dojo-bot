@echo off
cd /d "%~dp0"

echo Stopping the bot and disabling auto-start...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_service.ps1"

echo.
echo Launch the bot manually with start_bot.bat when you want it running.
echo.
pause