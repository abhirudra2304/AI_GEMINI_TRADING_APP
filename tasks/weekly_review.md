# Task for Antigravity: weekly review of generated reports

Read GEMINI.md first. Follow it. Read-only except the output file below.

## Goal
Summarize this week's generated reports so the user can see what changed without opening each file.

## Inputs (read only these)
- reports/daily_market_scan_*.md : the 5 newest files
- reports/avgdown_watch_*.md and reports/flow_watch_*.md : any that exist (names ending _SIGNAL.md first)
- journal/review_*.md : the newest file, if any
- reports/validation_*.md : the newest file

## Steps
1. List which of the above files exist and their dates. Say plainly if any are missing.
2. For the daily scans: note regime/breadth changes across the 5 days and any symbols repeating.
3. For the watch reports: list every STRICT/EARLY/SPIKE/deal line exactly as printed; if none, say "no signals".
4. For the journal review and validation: restate headline numbers (win rate, net return, n) without interpreting them as edge.
5. Do not open any *.csv, .env, or *.pkl file. Do not run any command.

## Output
Write to: reports/weekly_review_result.md
Format: max 250 words, then one table (date | regime | notable). Findings only, no filler.
State sample sizes and date ranges. Do not claim a signal a tool did not print. No buy/sell advice.

## Rules check (required, last section of the output)
- Files edited besides the output file: none / list
- Commands run: none / list
- Anything unclear or unverifiable: list
