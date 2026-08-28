# Runs at 14:25 on trading days: refreshes BTST/SWING discovery cache,
# pre-warms EMFB's result cache, AND produces a momentum report + result
# cache - so both the 15:15 eod run (run_eod.ps1) and a `stockscan` decision
# run hit warm caches / a today-dated report instead of re-fetching the full
# universe. See emfb.py/orchestrator.py for why this matters - most of an
# `eod` run's ~19 minutes (and a cold momentum scan's ~28) is uncached
# intraday candle fetches, which this eliminates by running them ~50 minutes
# earlier while there's no deadline pressure. stock_scan.py then reuses the
# today-dated momentum report in ~20s, well before the 15:20 decision cutoff.
$ErrorActionPreference = "Continue"
Set-Location "C:\AI_GEMINI_TRADING_APP"

$logDir = "C:\AI_GEMINI_TRADING_APP\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir "prewarm_$(Get-Date -Format 'yyyy-MM-dd').log"
$python = "C:\AI_GEMINI_TRADING_APP\.venv\Scripts\python.exe"
$lockFile = "C:\AI_GEMINI_TRADING_APP\main.pid"

"=== Prewarm run started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -FilePath $logFile -Append -Encoding utf8

# 2026-08-21: previously each step below hit main.py's single-instance lock,
# saw "Another instance is already running", and exited immediately - if a
# manual scan happened to still be running at 14:25, ALL FOUR steps silently
# no-op'd, leaving BTST/SWING discovery cache stale for the rest of the day
# with nothing but a buried log line to show for it (first caught 2026-08-21,
# discovery cache still dated 2 days old at EOD time). Wait for the lock to
# clear instead of giving up on first contact - there's ~50 minutes of slack
# before the 15:15 EOD deadline, so a bounded wait here is cheap insurance.
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
& $python main.py discover BTST --force-refresh *>&1 | Out-File -FilePath $logFile -Append -Encoding utf8

Wait-ForLock
& $python main.py discover SWING --force-refresh *>&1 | Out-File -FilePath $logFile -Append -Encoding utf8

Wait-ForLock
& $python main.py emfb *>&1 | Out-File -FilePath $logFile -Append -Encoding utf8

Wait-ForLock
& $python main.py momentum *>&1 | Out-File -FilePath $logFile -Append -Encoding utf8

"=== Prewarm run finished $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -FilePath $logFile -Append -Encoding utf8
