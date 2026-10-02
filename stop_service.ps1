# Stops the TwitchDiscordBot scheduled task, its restart wrapper (only if it
# is running run_forever.ps1 from this folder), and the bot process itself
# (identified by the single-instance port it holds from bot.py), then
# unregisters the task. Nothing is reported as stopped unless a follow-up
# check confirms each process/task is really gone.
$ErrorActionPreference = "SilentlyContinue"
$taskName = "TwitchDiscordBot"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$botScript = Join-Path $scriptDir "bot.py"

# Read the single-instance port from bot.py so this script never goes stale.
$port = 0
$botSource = Get-Content -LiteralPath $botScript -Raw
$match = [regex]::Match($botSource, "SINGLE_INSTANCE_PORT\s*=\s*(\d+)")
$portKnown = $match.Success
if ($portKnown) {
    $port = [int]$match.Groups[1].Value
}

$stopped = @()
$failures = @()
$notes = @()

# Wait about a second after a Stop-Process, then confirm the process is really
# gone with Get-Process -Id before calling it a success.
function Test-ProcessGone([int]$processId) {
    Start-Sleep -Seconds 1
    return ($null -eq (Get-Process -Id $processId -ErrorAction SilentlyContinue))
}

# Stop the task itself (kills the run_forever wrapper).
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $taskName
    $stopped += "$taskName scheduled task"
}

# Stop the bot by the port it holds (Listen state only), via Get-NetTCPConnection.
# Only kill it if its command line actually points at bot.py - another program
# might own the same port, and we must not kill it.
if ($port -gt 0) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    $botPid = ($listener | Select-Object -First 1 -ExpandProperty OwningProcess -ErrorAction SilentlyContinue)
    if ($botPid -and $botPid -ne $PID) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $botPid" -ErrorAction SilentlyContinue
        if ($owner -and $owner.CommandLine -and $owner.CommandLine.Contains("bot.py")) {
            Stop-Process -Id $botPid -Force -ErrorAction SilentlyContinue
            if (Test-ProcessGone ([int]$botPid)) {
                $stopped += "bot process (PID $botPid, port $port)"
            } else {
                $failures += "could not stop bot process (PID $botPid)"
            }
        } else {
            $notes += "port $port is held by another program (PID $botPid); left it alone"
        }
    }
}

# Kill a leftover run_forever wrapper, but only a PowerShell host whose command
# line actually contains run_forever.ps1 from this script's folder. The folder
# path is compared case-insensitively with .Contains() so wildcard characters
# in the path can't be misread by -like.
$folderMatch = $scriptDir
$tolerance = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe' OR Name = 'pwsh.exe'" |
    Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine.Contains("run_forever.ps1") -and $_.CommandLine.IndexOf($folderMatch, [StringComparison]::OrdinalIgnoreCase) -ge 0 }

foreach ($process in $tolerance) {
    $wrapperPid = [int]$process.ProcessId
    Stop-Process -Id $wrapperPid -Force -ErrorAction SilentlyContinue
    if (Test-ProcessGone $wrapperPid) {
        $stopped += "restart wrapper (PID $wrapperPid)"
    } else {
        $failures += "could not stop restart wrapper (PID $wrapperPid)"
    }
}

# Unregister so it won't come back at the next logon, and confirm it's gone.
$taskRemoved = $false
if ($task) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Start-Sleep -Seconds 1
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        $failures += "could not remove the $taskName scheduled task; auto-start may still be enabled"
    } else {
        $taskRemoved = $true
    }
}

if ($stopped.Count -gt 0) {
    Write-Host "Stopped:" -ForegroundColor Green
    foreach ($item in $stopped) {
        Write-Host "  - $item"
    }
    Write-Host ""
}

if ($taskRemoved) {
    Write-Host "Auto-start disabled: the scheduled task was removed." -ForegroundColor Green
}

if (-not $portKnown) {
    Write-Host "Note: could not read SINGLE_INSTANCE_PORT from bot.py; only the task and wrapper were checked." -ForegroundColor Yellow
} elseif ($stopped.Count -eq 0 -and $failures.Count -eq 0) {
    Write-Host "Nothing was running - the bot and the auto-start were already stopped." -ForegroundColor Yellow
}

foreach ($failure in $failures) {
    Write-Host "  - $failure" -ForegroundColor Yellow
}
foreach ($note in $notes) {
    Write-Host "  - $note" -ForegroundColor Yellow
}
Write-Host "Launch it manually with start_bot.bat when you want it running."