@echo off
setlocal
cd /d "%~dp0"

rem Read the single-instance port from bot.py so this script never goes stale.
for /f "delims=" %%P in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$m = [regex]::Match((Get-Content -LiteralPath '%~dp0bot.py' -Raw), 'SINGLE_INSTANCE_PORT\s*=\s*(\d+)'); if ($m.Success) { $m.Groups[1].Value }"') do set "PORT=%%P"
if not defined PORT set "PORT=47821"

rem Ask about auto-start only if the scheduled task isn't installed yet.
schtasks /Query /TN TwitchDiscordBot >nul 2>&1
if %errorlevel%==0 goto task_installed

echo The bot is NOT set to auto-start when Windows starts.
set /p AUTOSTART="Set it to auto-start at Windows logon? (Y/N): "
if /i "%AUTOSTART%"=="Y" goto install_task
goto check_running

:install_task
echo.
echo Installing auto-start task...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_service.ps1"
echo.
goto check_running

:task_installed
echo Auto-start task 'TwitchDiscordBot' is already installed.

:check_running
rem The bot holds a localhost port while it runs; skip launch if it is listening.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$c = Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if ($null -eq $c) { exit 1 } else { exit 0 }"
if %errorlevel%==0 goto already_running

python -m pip install -r requirements.txt
python bot.py
goto done

:already_running
echo.
echo A bot instance is already running, so launch was skipped.
goto done

:done
pause
endlocal