---
description: Run EOD scan, score previous picks vs outcomes, log TARGET/STOP to journal/signals.csv
allowed-tools: Bash, Read
---
End-of-day workflow. Findings only, non-advisory.

1. `.venv/Scripts/python.exe main.py eod` (one-shot; persists BTST/SWING signals to signals.db). If the arguments contain `skip-scan`, skip this step (the scheduled task already ran it). Skip if the market is closed (`market_calendar.py`) and say so.
2. Score earlier picks: `.venv/Scripts/python.exe journal_signals.py` (reads signals.db + cached daily bars, appends newly resolved rows to `journal/signals.csv`; idempotent). Use `--dry-run` first if asked to preview. Outcomes: TARGET, STOP (stop wins if one daily bar touches both), TIMEOUT, NO_FILL.
3. Print: counts by strategy (TARGET/STOP/TIMEOUT/NO_FILL), mean R, and the list of resolved symbols. Do not Read *.csv directly (denied); use the script output or `python -c`.
4. Compare with the previous day's picks: list yesterday's BTST/GAP/SWING picks from signals.db with today's close vs trigger/stop/target.
5. Note data caveats (daily-bar resolution, stale caches) in one line. Do not draw performance conclusions from fewer than 30 resolved trades.
