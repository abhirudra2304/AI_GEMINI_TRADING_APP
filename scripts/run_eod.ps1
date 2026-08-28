# Runs at 15:15 on trading days: the actual eod scan. Relies on
# prewarm_eod.ps1 (14:25) having already warmed the Discovery/EMFB caches, so
# this should finish in ~1-2 minutes instead of ~19, leaving real margin
# before the 15:30 close. main.py's run_eod() itself saves a PDF report and
# fires a Windows notification when done - nothing else here needs to.
$ErrorActionPreference = "Continue"
Set-Location "C:\AI_GEMINI_TRADING_APP"

$logDir = "C:\AI_GEMINI_TRADING_APP\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir "eod_$(Get-Date -Format 'yyyy-MM-dd').log"
$python = "C:\AI_GEMINI_TRADING_APP\.venv\Scripts\python.exe"
$lockFile = "C:\AI_GEMINI_TRADING_APP\main.pid"

"=== EOD run started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -FilePath $logFile -Append -Encoding utf8

# 2026-08-24: this used to hit main.py's single-instance lock and exit
# immediately if anything else held it at 15:15 - discovered when a live
# scanner test session (main.py live, fixed/validated earlier the same day)
# was still running through the afternoon and held the lock straight through
# EOD's trigger time, silently skipping BTST/SWING/EMFB entirely for the day
# with only "Another instance is already running" buried in the log. Same
# fix as prewarm_eod.ps1's Wait-ForLock: EOD has no hard downstream deadline
# like Prewarm feeding the ~14:55 decision window (it's an end-of-day report,
# fine to land a few minutes late), so a bounded wait here is cheap insurance
# against exactly this collision recurring.
function Wait-ForLock {
    param([int]$MaxWaitSeconds = 900, [int]$PollSeconds = 20)
    $waited = 0
    while ((Test-Path $lockFile) -and ($waited -lt $MaxWaitSeconds)) {
        "  Lock file present (main.pid) - waiting ${PollSeconds}s for it to clear (waited ${waited}s/${MaxWaitSeconds}s)..." | Out-File -FilePath $logFile -Append -Encoding utf8
        Start-Sleep -Seconds $PollSeconds
        $waited += $PollSeconds
    }
    if (Test-Path $lockFile) {
        "  WARNING: lock still held after ${MaxWaitSeconds}s wait - proceeding anyway, step below may no-op." | Out-File -FilePath $logFile -Append -Encoding utf8
    }
}

Wait-ForLock
& $python main.py eod *>&1 | Out-File -FilePath $logFile -Append -Encoding utf8

"=== EOD run finished $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -FilePath $logFile -Append -Encoding utf8
