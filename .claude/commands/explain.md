---
description: Factor-by-factor Rank_Score breakdown for one symbol (usage /explain SYMBOL)
allowed-tools: Bash
argument-hint: SYMBOL [BTST|SWING|GAP|INTRADAY]
---
Run `.venv/Scripts/python.exe explain_rank.py $ARGUMENTS` and show the output unchanged. If the symbol is not in any cached frame, report the FILTER NOTE (it was dropped by discovery hard filters or the cache is stale) and stop; do not guess values. Read-only: no scans, no API calls.
