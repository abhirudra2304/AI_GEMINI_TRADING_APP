# Runs at 14:30 on trading days: the decision scan (momentum with a real post-14:30
# volume basis) followed by the BTST/SWING/GRIND brief, landing before the 15:15
# closing auction. Replaces a session-held background job that died whenever the
# Claude session restarted (2026-10-14: no post-14:30 scan ran at all).
$ErrorActionPreference = "Continue"
Set-Location "C:\AI_GEMINI_TRADING_APP"

$logDir = "C:\AI_GEMINI_TRADING_APP\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$day = Get-Date -Format 'yyyy-MM-dd'
$logFile = Join-Path $logDir "decision_$day.log"
$briefFile = Join-Path $logDir "brief_$day.log"
$python = "C:\AI_GEMINI_TRADING_APP\.venv\Scripts\python.exe"
$lockFile = "C:\AI_GEMINI_TRADING_APP\main.pid"

function Log($msg) { "$(Get-Date -Format 'HH:mm:ss') $msg" | Out-File -FilePath $logFile -Append -Encoding utf8 }

# Prewarm (13:15) normally finishes by ~14:15 but can run long on a bad API day.
function Wait-ForLock {
    param([int]$MaxWaitSeconds = 1800, [int]$PollSeconds = 15)
    $waited = 0
    while ((Test-Path $lockFile) -and ($waited -lt $MaxWaitSeconds)) {
        Start-Sleep -Seconds $PollSeconds
        $waited += $PollSeconds
    }
    if (Test-Path $lockFile) { Log "WARNING: lock still held after ${MaxWaitSeconds}s - proceeding anyway, step may no-op." }
    elseif ($waited -gt 0) { Log "lock cleared after ${waited}s" }
}

Log "=== Decision run started ==="
Wait-ForLock
Log "momentum --force-refresh"
& $python main.py momentum --force-refresh *>&1 | Out-File -FilePath $logFile -Append -Encoding utf8
Wait-ForLock -MaxWaitSeconds 120 -PollSeconds 3
Log "brief"
& $python main.py brief *>&1 | Out-File -FilePath $briefFile -Encoding utf8
Log "=== Decision run finished (brief: $briefFile) ==="
