# One-off diagnostic runner for scripts/cas_probe.py, launched via a one-time
# Task Scheduler entry (TradingApp_CASProbe_OneOff) instead of a long-lived
# background process, because the session's own background-task lifetime was
# killing the probe before it reached the 15:15-15:35 CAS window.
$ErrorActionPreference = "Continue"
Set-Location "C:\AI_GEMINI_TRADING_APP"

$logDir = "C:\AI_GEMINI_TRADING_APP\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir "cas_probe_run_$(Get-Date -Format 'yyyy-MM-dd').log"
$python = "C:\AI_GEMINI_TRADING_APP\.venv\Scripts\python.exe"

"=== CAS probe started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -FilePath $logFile -Append -Encoding utf8
& $python scripts\cas_probe.py *>&1 | Out-File -FilePath $logFile -Append -Encoding utf8
"=== CAS probe finished $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -FilePath $logFile -Append -Encoding utf8
