---
description: GAP + BTST scan, news/filings check per hit, ranked brief to briefs/YYYY-MM-DD_premarket.md
allowed-tools: Bash, Read, Write, WebSearch, Agent
---
Run the pre-market workflow. Terminal-style, non-advisory, findings only. NEVER place or suggest placing orders.

1. Scans (one-shot modes only; raw `btst|swing|gap|intraday` hang and are hook-blocked):
   - `.venv/Scripts/python.exe main.py gapscan`
   - `.venv/Scripts/python.exe main.py brief` (BTST/SWING/GRIND, includes trigger sanity + tradability)
   - Read results via stdout or `python -c` with pandas. Do not Read *.csv directly (denied).
2. Pick up to 10 candidates (BTST + GAP first, then SWING/GRIND). Drop NO_BTST / NEEDS VERIFICATION rows into a separate section.
3. Per candidate, WebSearch (last 7 days): news, exchange filings, corporate actions (split/bonus/dividend/ex-date), promoter pledge, SEBI action, block/bulk deals. Check `earnings_manager.py` data first for result dates. Flag `RESULT<=5TD` when a results date falls within the next 5 trading days (use `market_calendar.py` for trading days).
4. Optionally delegate red-flag checks to the `news-checker` agent once it exists.
5. Write `briefs/<YYYY-MM-DD>_premarket.md` (mkdir if missing), fixed-width, ASCII rules (108 cols, like decision_brief.py):
   - Header `PREMARKET BRIEF - <date> <time> IST`, then one regime line (breadth/AD ratio from the brief, not RS dispersion).
   - Ranked table: `# | Symbol | Source | Setup | Conviction(1-10) | R:R | Trigger/Stop/Target | Flags`.
   - Conviction = mean of Setup Quality, Regime Alignment, Risk-Reward, Liquidity-Executability (1-10 each). Never drop High-conviction rows when truncating.
   - Section `RED FLAGS` (per symbol, source named), section `RESULTS <=5TD`, section `WHAT I DID NOT FIND`.
   - Footer: data timestamps, any stale-data warnings, "Informational only. Not investment advice. No proven edge in these signals."
6. Print the path and the top 5 rows only.
