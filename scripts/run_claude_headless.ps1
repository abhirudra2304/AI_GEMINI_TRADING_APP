# Headless Claude Code run of /premarket or /eod (Stage 5). Read-only analysis: the project hooks
# (.claude/settings.json) stay active, so order calls and strategy-file edits remain blocked. Never use --bare here.
#   -Mode premarket : runs after prewarm_eod.ps1 (caches warm). Writes briefs/<date>_premarket.md
#   -Mode eod       : runs after run_eod.ps1 (main.py eod already done) -> journal_signals.py only
#   -CheckOnly      : log the gates and the exact command, run nothing
param(
    [Parameter(Mandatory = $true)][ValidateSet("premarket", "eod")][string]$Mode,
    [switch]$CheckOnly,
    [int]$TimeoutMinutes = 25
)
$ErrorActionPreference = "Continue"
$root = "C:\AI_GEMINI_TRADING_APP"
Set-Location $root
$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir "claude_${Mode}_$(Get-Date -Format 'yyyy-MM-dd').log"
$python = Join-Path $root ".venv\Scripts\python.exe"
$claude = "C:\Users\abhir\.local\bin\claude.exe"
$lockFile = Join-Path $root "main.pid"
function Log($m) { "$m" | Out-File -FilePath $logFile -Append -Encoding utf8 }

Log "=== claude headless [$Mode] started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==="

# NSE holiday gate (exit 3 = closed)
& $python market_calendar.py *>&1 | Out-File -FilePath $logFile -Append -Encoding utf8
if ($LASTEXITCODE -eq 3) { Log "=== NSE closed today - skipping ==="; exit 0 }

# CAS window 15:15-15:35 breaks F&O data: premarket must not start inside it (eod is cache/db-only, allowed)
$now = Get-Date
if ($Mode -eq "premarket" -and $now.TimeOfDay -ge [TimeSpan]"15:15" -and $now.TimeOfDay -lt [TimeSpan]"15:35") {
    Log "=== inside CAS window 15:15-15:35 - skipping premarket ==="; exit 0
}

# Same single-instance lock the other tasks honour: wait up to 15 min for main.pid to clear
$waited = 0
while ((Test-Path $lockFile) -and $waited -lt 900) { Log "  main.pid present - waiting 20s ($waited/900)"; Start-Sleep -Seconds 20; $waited += 20 }
if (Test-Path $lockFile) { Log "  WARNING: lock still held after 900s - proceeding anyway" }

$now = Get-Date   # re-check CAS window: the lock wait may have run into it
if ($Mode -eq "premarket" -and $now.TimeOfDay -ge [TimeSpan]"15:15" -and $now.TimeOfDay -lt [TimeSpan]"15:35") {
    Log "=== lock wait ran into CAS window 15:15-15:35 - skipping premarket ==="; exit 0
}

if ($Mode -eq "premarket") {
    $prompt = "/premarket"
    $tools = "Bash(.venv/Scripts/python.exe:*) Bash(mkdir:*) Read Write(briefs/**) WebSearch Agent"
} else {
    $prompt = "/eod skip-scan"   # run_eod.ps1 already ran main.py eod
    $tools = "Bash(.venv/Scripts/python.exe:*) Read"
}
# Start-Process joins the array without quoting, so quote values that contain spaces
$argList = @("-p", "`"$prompt`"", "--allowedTools", "`"$tools`"", "--output-format", "text")
Log "  cmd: claude $($argList -join ' ')"
if ($CheckOnly) { Log "=== CheckOnly - nothing run ==="; exit 0 }

$out = Join-Path $logDir "claude_${Mode}_stdout.tmp"
$err = Join-Path $logDir "claude_${Mode}_stderr.tmp"
$p = Start-Process -FilePath $claude -ArgumentList $argList -WorkingDirectory $root -NoNewWindow -PassThru `
        -RedirectStandardOutput $out -RedirectStandardError $err
if (-not $p.WaitForExit($TimeoutMinutes * 60 * 1000)) {
    $timedOut = $true
    Log "  TIMEOUT after $TimeoutMinutes min - killing claude"
    & taskkill /PID $p.Id /T /F *>&1 | Out-Null   # whole tree, so no orphan python keeps main.pid
}
$p.WaitForExit()   # populates ExitCode
Get-Content $out -ErrorAction SilentlyContinue | Out-File -FilePath $logFile -Append -Encoding utf8
$e = Get-Content $err -ErrorAction SilentlyContinue
if ($e) { Log "--- stderr ---"; $e | Out-File -FilePath $logFile -Append -Encoding utf8 }
Remove-Item $out, $err -ErrorAction SilentlyContinue
Log "=== claude headless [$Mode] finished $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') exit=$($p.ExitCode) ==="
if ($timedOut) { exit 2 }
if ($p.ExitCode -ne 0) { exit 1 }
