# Wrapper run by the TwitchDiscordBot scheduled task. Starts bot.py with
# python.exe (not pythonw, which disappears without a window) and WAITS for it
# to exit, restarting it after a short delay, unless the exit is the "already
# running" case (another copy holds the single-instance port) - then it stops
# quietly.
#
# All output is redirected to logs\bot.out and logs\bot.err; every start/exit
# is timestamped into logs\wrapper.log. After each exit, any non-empty
# bot.out/bot.err content is appended to logs\crash-history.log (rotated to
# crash-history.log.old past 512 KB), so a crash traceback isn't lost when the
# next restart truncates the redirects. The restart delay is slept first and
# doubled afterwards: 10 seconds after the first quick failure, then 20, 40,
# 80, 160, capped at 300. Once the bot has run for at least 120 seconds the
# next sleep is 10 seconds again and the sequence starts over.

$ErrorActionPreference = "Continue"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$logsDir = Join-Path $scriptDir "logs"

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

function Write-Log([string]$message) {
    $line = ("{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message)
    Add-Content -LiteralPath (Join-Path $logsDir "wrapper.log") -Value $line
}

function Append-CrashCapture([string]$capturePath, [string]$label, [int]$exitCode) {
    if (-not (Test-Path -LiteralPath $capturePath)) { return }
    $capture = Get-Item -LiteralPath $capturePath
    if ($capture.Length -eq 0) { return }

    $history = Join-Path $logsDir "crash-history.log"
    if (Test-Path -LiteralPath $history) {
        if ((Get-Item -LiteralPath $history).Length -gt 512KB) {
            $old = Join-Path $logsDir "crash-history.log.old"
            Remove-Item -LiteralPath $old -Force -ErrorAction SilentlyContinue
            Move-Item -LiteralPath $history -Destination $old -Force -ErrorAction SilentlyContinue
        }
    }
    Add-Content -LiteralPath $history `
        -Value ("=== {0} : {1}, exit code {2} ===" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $label, $exitCode)
    Add-Content -LiteralPath $history -Value (Get-Content -LiteralPath $capturePath)
    Write-Log ("Appended {0} to crash-history.log (exit code {1})." -f $label, $exitCode)
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Log "Python not found on PATH. Wrapper cannot run the bot."
    exit 1
}
$botScript = Join-Path $scriptDir "bot.py"
$outFile = Join-Path $logsDir "bot.out"
$errFile = Join-Path $logsDir "bot.err"
$exitAlreadyRunning = 3
$delaySeconds = 10
$maxDelaySeconds = 300
$resetDelayAfterSeconds = 120

while ($true) {
    Write-Log "Starting bot.py..."
    # The path is passed with embedded double quotes, so a folder path that
    # contains spaces or parentheses still launches correctly.
    $process = Start-Process -FilePath $python.Source -ArgumentList "`"$botScript`"" `
        -WorkingDirectory $scriptDir -RedirectStandardOutput $outFile `
        -RedirectStandardError $errFile -WindowStyle Hidden -Wait -PassThru
    $code = $process.ExitCode
    if ($code -eq $exitAlreadyRunning) {
        Write-Log "A bot instance is already running (exit code 3); stopping the wrapper."
        exit 0
    }

    # bot.out / bot.err are truncated when the next Start-Process begins, so
    # copy anything the dying bot printed out first.
    Append-CrashCapture $errFile "bot.err" $code
    Append-CrashCapture $outFile "bot.out" $code

    $duration = [int](($process.ExitTime - $process.StartTime).TotalSeconds)
    if ($duration -lt 0) { $duration = 0 }

    # A long run resets the delay ladder so the next restart waits 10 s again.
    if ($duration -ge $resetDelayAfterSeconds) {
        $delaySeconds = 10
    }

    Write-Log ("bot.py exited with code {0} after {1}s; restarting in {2}s." -f $code, $duration, $delaySeconds)
    Start-Sleep -Seconds $delaySeconds

    # Double AFTER the sleep, so the first restart really waits 10 s, then
    # 20, 40, 80, 160, and caps at 300.
    $delaySeconds = [Math]::Min($delaySeconds * 2, $maxDelaySeconds)
}