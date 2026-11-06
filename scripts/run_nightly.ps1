# Runs at 20:15 on trading days, after NSE publishes the day's bhavcopy:
#  1. pull NSE delivery/bhavcopy files (strategy_paper.py update)
#  2. overwrite cached daily closes with NSE official closes (apply_official_closes.py)
#  3. score signals that have no final outcome yet (score_new_outcomes.py)
# None of these touch main.py's lock or the broker API.
$ErrorActionPreference = "Continue"
Set-Location "C:\AI_GEMINI_TRADING_APP"

$logDir = "C:\AI_GEMINI_TRADING_APP\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir "nightly_$(Get-Date -Format 'yyyy-MM-dd').log"
$python = "C:\AI_GEMINI_TRADING_APP\.venv\Scripts\python.exe"

function Log($msg) { "$(Get-Date -Format 'HH:mm:ss') $msg" | Out-File -FilePath $logFile -Append -Encoding utf8 }

Log "=== Nightly run started ==="
#  4. journal.py: one row per day + watch signals to journal/ (2026-11-06)
#  5. Fridays: journal_review.py --days 7 -> journal/review_<date>.md
$steps = @("strategy_paper.py update", "apply_official_closes.py", "score_new_outcomes.py", "fii_dii_log.py", "journal.py")
if ((Get-Date).DayOfWeek -eq 'Friday') { $steps += "journal_review.py --days 7" }
foreach ($step in $steps) {
    Log "-> $step"
    $parts = $step.Split(" ")
    & $python @parts *>&1 | Out-File -FilePath $logFile -Append -Encoding utf8
    Log "   exit $LASTEXITCODE"
}
Log "=== Nightly run finished ==="
