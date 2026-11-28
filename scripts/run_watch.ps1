# Zero-token replacement for the Claude scheduled tasks avgdown-watch-daily (15:40) and flow-watch-daily (20:45).
# Runs the watch scripts directly, writes reports/<mode>_watch_<date>.md, and flags signals in the file name
# (suffix _SIGNAL) plus a best-effort desktop balloon. Read-only: no orders, no strategy edits.
#   -Mode avgdown : avgdown_watch.py --live (held names) + pullback watch (financials)
#   -Mode flow    : flow_watch.py NETWEB SOLARINDS (needs today's delivery file from the 20:15 nightly)
param([Parameter(Mandatory = $true)][ValidateSet("avgdown", "flow")][string]$Mode)
$ErrorActionPreference = "Continue"
$root = "C:\AI_GEMINI_TRADING_APP"
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
$date = Get-Date -Format 'yyyy-MM-dd'
$repDir = Join-Path $root "reports"
if (-not (Test-Path $repDir)) { New-Item -ItemType Directory -Path $repDir | Out-Null }
$report = Join-Path $repDir "${Mode}_watch_${date}.md"

& $python market_calendar.py *>&1 | Out-Null
if ($LASTEXITCODE -eq 3) { "NSE closed $date - skipped" | Out-File $report -Encoding utf8; exit 0 }

$noise = 'INFO|WARNING|^\[|Stage|in pool|^\s*$|getCandle'
$body = @()
if ($Mode -eq "avgdown") {
    $body += "## HELD (average-down)"
    $body += (& $python avgdown_watch.py --live 2>&1 | Where-Object { $_ -notmatch $noise })
    $body += ""
    $body += "## PULLBACK WATCH (financials)"
    $body += (& $python avgdown_watch.py ICICIBANK HDFCBANK KOTAKBANK AXISBANK RELIANCE SHRIRAMFIN MUTHOOTFIN --live 2>&1 | Where-Object { $_ -notmatch $noise })
    $hit = ($body | Select-String -Pattern 'signal (STRICT|EARLY)')
} else {
    $deliv = Join-Path $root ("delivery_cache\" + (Get-Date -Format 'yyyyMMdd') + ".parquet")
    if (-not (Test-Path $deliv)) {
        # nightly (20:15) may not have published yet: run the same update step once, then recheck
        & $python strategy_paper.py update *>&1 | Out-Null
    }
    if (-not (Test-Path $deliv)) { "Delivery file for today missing after update - no flow report" | Out-File $report -Encoding utf8; exit 1 }
    $body += (& $python flow_watch.py NETWEB SOLARINDS 2>&1 | Where-Object { $_ -notmatch 'INFO|WARNING' })
    $hit = ($body | Select-String -Pattern 'SPIKE: (?!-)|^\S+ .*(BULK|BLOCK)\b' )
}
$header = "# $Mode watch $date (generated $(Get-Date -Format 'HH:mm'))`n"
if ($hit) { $header += "**SIGNAL PRESENT** - review below. Candidate to review, not a buy call.`n" }
($header + "`n" + ($body -join "`n")) | Out-File $report -Encoding utf8
if ($hit) {
    Rename-Item $report ($report -replace '\.md$', '_SIGNAL.md') -Force
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $n = New-Object System.Windows.Forms.NotifyIcon
        $n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true
        $n.ShowBalloonTip(10000, "$Mode watch", "Signal found - see reports\${Mode}_watch_${date}_SIGNAL.md", 'Info')
        Start-Sleep -Seconds 12; $n.Dispose()
    } catch {}
}
