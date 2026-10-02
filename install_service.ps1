# Installs the 'TwitchDiscordBot' scheduled task. At your next logon it runs
# run_forever.ps1 (hidden), which starts bot.py and restarts it after a
# growing delay (10s up to 5 minutes) whenever it exits (unless another copy
# is already running).

$ErrorActionPreference = "Stop"

$taskName = "TwitchDiscordBot"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$botScript = Join-Path $scriptDir "bot.py"
$logsDir = Join-Path $scriptDir "logs"

# Locate Python; the Microsoft Store 'python.exe' stub is not a real install.
# run_forever.ps1 uses the real python.exe (visible stderr/stdout) and blocks
# until the bot exits, so no pythonw lookup is needed here.
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw "Python not found on PATH. Install Python from python.org and try again."
}
$pythonPath = $python.Source
if ($pythonPath -like "*WindowsApps*") {
    Write-Warning "python resolves to the Microsoft Store stub (WindowsApps). If a real Python isn't installed, the bot won't run. Install Python from python.org and make sure it comes first on PATH."
}
if (-not (Test-Path $botScript)) {
    throw "bot.py not found in $scriptDir"
}

$wrapper = Join-Path $scriptDir "run_forever.ps1"
if (-not (Test-Path $wrapper)) {
    throw "run_forever.ps1 not found in $scriptDir"
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$wrapper`"" `
    -WorkingDirectory $scriptDir

$trigger = New-ScheduledTaskTrigger -AtLogOn

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

# Best-effort start; if a previous instance is running, stop it first.
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings (New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew) | Out-Null

Write-Host "Installed scheduled task '$taskName'." -ForegroundColor Green
Write-Host "It auto-starts at your next logon (run_forever.ps1 keeps bot.py alive)."
Write-Host "Python:  $pythonPath"
Write-Host "Logs:    $logsDir\bot.log (bot), $logsDir\bot.out / $logsDir\bot.err (wrapper captures), $logsDir\wrapper.log, $logsDir\crash-history.log"
Write-Host ""
Write-Host "Start it now:  Start-ScheduledTask -TaskName $taskName"
Write-Host "Check status:  Get-ScheduledTask -TaskName $taskName | Select State"
Write-Host "Uninstall:     Unregister-ScheduledTask -TaskName $taskName -Confirm:`$false"